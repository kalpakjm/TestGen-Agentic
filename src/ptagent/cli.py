"""Command line interface: python run.py <command> ..."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agents.orchestrator import Orchestrator
from .config import REPO_ROOT, load_config
from .llm import BACKEND_NAMES
from .preflight import UsageError, check_backend, check_project, check_versions
from .evaluation.evaluate import evaluate
from .tools.analyzer import build_index

SAMPLES = REPO_ROOT / "data" / "sample_projects"


def _config(args):
    cfg = load_config(Path(args.config) if args.config else None)
    if getattr(args, "backend", None):
        cfg.llm.backend = args.backend
    if getattr(args, "model", None) and cfg.llm.backend != "offline":
        setattr(cfg.llm, f"{cfg.llm.backend}_model", args.model)
    if cfg.llm.backend in ("openai", "gemini", "anthropic"):
        print(f"note: backend '{cfg.llm.backend}' sends your function source code to that provider's API "
              f"(key from environment variable, usage is billed by the provider).", file=sys.stderr)
    if getattr(args, "output_dir", None):
        cfg.output_dir = Path(args.output_dir).resolve()
    return cfg


def _notes_options(args) -> dict:
    return {"rules": getattr(args, "rules", None), "local_dir": getattr(args, "release_notes_dir", None),
            "offline": getattr(args, "offline", False), "refresh": getattr(args, "refresh_notes", False)}


def _add_notes_flags(parser):
    parser.add_argument("--rules", choices=["dynamic", "curated", "both"],
                        help="dynamic: read the official release notes (default); curated: use knowledge/dbr_rules.json")
    parser.add_argument("--release-notes-dir", help="folder with saved release-note pages named 17.3lts.html, 16.0.md ...")
    parser.add_argument("--offline", action="store_true", help="do not use the network; use saved pages and the cache only")
    parser.add_argument("--refresh-notes", action="store_true", help="ignore the cache and fetch the pages again")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run.py", description="Multi-agent test generation for PySpark on Databricks")
    parser.add_argument("--config", help="path to a config YAML (default: configs/config.yaml)")
    parser.add_argument("--debug", action="store_true", help="show the full Python traceback when something fails")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="index a project and print a summary")
    analyze.add_argument("project")

    run = sub.add_parser("run", help="run the agents on a project")
    run.add_argument("project")
    run.add_argument("--mode", choices=["auto", "A", "B", "C"], default="auto")
    run.add_argument("--from-dbr", help="current Databricks Runtime, e.g. 13.3")
    run.add_argument("--to-dbr", help="target Databricks Runtime, e.g. 17.3")
    run.add_argument("--backend", choices=BACKEND_NAMES)
    run.add_argument("--model", help="model name for the chosen backend (overrides the config file)")
    run.add_argument("--output-dir")
    run.add_argument("--fail-under", type=float, help="exit with code 1 if final coverage is below this percentage")
    run.add_argument("--apply-in-place", action="store_true",
                     help="Mode B: copy the cleanup over your sources (keeps .bak files; asks first)")
    run.add_argument("--yes", action="store_true", help="do not ask for confirmation with --apply-in-place")
    _add_notes_flags(run)

    demo = sub.add_parser("demo", help="run all three modes on the bundled sample projects")
    demo.add_argument("--backend", choices=BACKEND_NAMES)
    demo.add_argument("--model", help="model name for the chosen backend (overrides the config file)")
    demo.add_argument("--output-dir")
    _add_notes_flags(demo)

    notes = sub.add_parser("notes", help="read the official release notes for an upgrade and show what was found")
    notes.add_argument("--from-dbr", required=True)
    notes.add_argument("--to-dbr", required=True)
    notes.add_argument("--output-dir")
    _add_notes_flags(notes)

    ev = sub.add_parser("evaluate", help="run the baseline evaluation on the labelled samples")
    ev.add_argument("--backend", choices=BACKEND_NAMES)
    ev.add_argument("--model", help="model name for the chosen backend (overrides the config file)")
    ev.add_argument("--output-dir")
    return parser


def _print_summary(report: dict):
    print(f"[{report['project']}] mode {report['mode_resolved']} (backend: {report['llm_backend']})")
    generation = report.get("generation")
    if generation:
        execution = generation["execution"]
        ran = (f"{execution['passed']} passed / {execution['failed']} failed / {execution.get('errors', 0)} errors, "
               f"coverage {execution.get('coverage_percent')}%"
               if execution.get("status") == "ran" else f"not executed ({execution.get('reason')})")
        print(f"  tests generated: {generation['tests_generated']} | skipped functions: {len(generation['skipped'])} | {ran}")
    for warning in (generation or {}).get("warnings", []):
        print(f"  WARNING: {warning}")
    if report.get("obsolete_code"):
        o = report["obsolete_code"]
        print(f"  obsolete: {len(o['dead_functions'])} dead functions, {len(o['stale_test_imports'])} stale test imports")
    if report.get("coverage_gaps"):
        print(f"  untested functions: {len(report['coverage_gaps']['untested_functions'])}")
    if report.get("coverage"):
        c = report["coverage"]
        def pct(value):
            return "n/a" if value is None else f"{value}%"

        print(f"  coverage: existing tests {pct(c['baseline_percent'])} -> with generated tests {pct(c['after_percent'])} "
              f"-> after cleanup {pct(c.get('after_cleanup_percent'))} | target {c['target_percent']}%: {c.get('verdict')}")
    if report.get("migration"):
        m = report["migration"]
        target = m["existing_tests"]["target_runtime"]
        existing = (f"{len(m['broken_by_upgrade'])} existing tests broken by the upgrade"
                    if target.get("status") == "ran" else f"existing tests not run ({target.get('reason')})")
        print(f"  migration: {len(m['migrated_tests'])} new tests for {len(m['migrated_functions'])} functions | "
              f"{existing} | {len(m['review_items'])} items to review by hand")
    if report.get("runtime_impact"):
        r = report["runtime_impact"]
        print(f"  runtime impact DBR {r['from_dbr']} -> {r['to_dbr']}: {len(r['findings'])} findings in "
              f"{len(r['impacted_files'])} files; {len(r['tests_to_review'])} test files to review")


def _show_notes(cfg, args) -> int:
    from .agents.base import Trace
    from .agents.release_notes import ReleaseNotesAgent

    options = _notes_options(args)
    agent = ReleaseNotesAgent(Trace(), cfg)
    brief = agent.build_brief(args.from_dbr, args.to_dbr, options["local_dir"], options["offline"], options["refresh"])
    rules, advisories, review = agent.rules(brief)
    print(f"Release notes for DBR {brief.from_version} -> {brief.to_version}")
    for v in brief.versions:
        print(f"  {v.version:>5} [{v.origin}] Spark {v.spark or '?'} | Python {v.environment.get('python', '?')} | "
              f"Scala {v.environment.get('scala', '?')} | {len(v.changes)} behavioral changes")
    for key, change in brief.env_diff().items():
        if change["from"] != change["to"]:
            print(f"  environment: {key}: {change['from']} -> {change['to']}")
    print(f"  Spark migration guide items: {len(brief.guide_changes)}")
    print(f"  rules derived: {len(rules)} | notes without a code pattern (advisories): {len(advisories)}")
    for rule in rules:
        shown = [n for m in rule["matchers"] for n in (m.get("names") or m.get("substrings") or [m["type"]])][:3]
        emulate = f" | emulates {rule['emulation']['conf']}={rule['emulation']['value']}" if rule.get("emulation") else ""
        print(f"    {rule['id']:<10} {rule['severity']:<6} {rule['title'][:70]} | matches {shown}{emulate}")
    if advisories:
        print("  notes with no code pattern (first 20; all are in the JSON file):")
        for item in advisories[:20]:
            flag = "BREAKING " if item.get("breaking") else ""
            print(f"    {item['version']:>5} {flag}{item['title'][:90]}")
    for warning in brief.warnings:
        print(f"  WARNING: {warning}")
    out = (Path(args.output_dir).resolve() if args.output_dir else cfg.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    target = out / f"release_notes_{brief.from_version}_to_{brief.to_version}.json"
    target.write_text(json.dumps({"brief": brief.summary(), "rules": rules, "advisories": advisories,
                                  "review_items": review}, indent=2, default=str), encoding="utf-8")
    print(f"  written: {target}")
    return 0 if brief.versions else 1


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _main(args)
    except KeyboardInterrupt:
        print("stopped by the user", file=sys.stderr)
        return 130
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # anything unexpected: one clear line, the traceback only on request
        if getattr(args, "debug", False):
            raise
        print(f"error: ptagent stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("  The run_trace.json in the output folder shows how far it got. "
              "Run again with --debug before the command (for example: ptagent --debug run ...) for the full traceback.",
              file=sys.stderr)
        return 1


def _main(args) -> int:
    cfg = _config(args)
    if getattr(args, "project", None):
        check_project(args.project)
    check_versions(getattr(args, "from_dbr", None), getattr(args, "to_dbr", None))
    if args.command in ("run", "demo", "evaluate"):
        for warning in check_backend(cfg.llm):
            print(f"WARNING: {warning}", file=sys.stderr)
    if args.command == "analyze":
        print(json.dumps(build_index(args.project, cfg.skip_dirs).summary(), indent=2))
        return 0
    if args.command == "run":
        def confirm(count):
            if args.yes:
                return True
            return input(f"Copy cleaned versions of {count} files over your sources (.bak files are kept)? [y/N] ").strip().lower() in ("y", "yes")

        report = Orchestrator(cfg).run(Path(args.project), args.mode, args.from_dbr, args.to_dbr,
                                       apply_in_place=args.apply_in_place, confirm=confirm, notes=_notes_options(args))
        _print_summary(report)
        print(f"  outputs: {cfg.output_dir / report['project']}")
        if args.fail_under is not None:
            final = (report.get("coverage") or {}).get("final_percent")
            if final is None:
                final = ((report.get("generation") or {}).get("execution") or {}).get("coverage_percent")
            if final is None:
                print("  coverage was not measured, so --fail-under cannot be checked")
                return 2
            if final < args.fail_under:
                print(f"  FAIL: coverage {final}% is below {args.fail_under}%")
                return 1
        return 0
    if args.command == "demo":
        for name, mode, versions in (("orders_no_tests", "A", (None, None)),
                                     ("orders_with_tests", "B", (None, None)),
                                     ("orders_ansi_demo", "C", ("15.4", "17.3")),
                                     ("legacy_runtime", "C", ("15.4", "17.3"))):
            report = Orchestrator(cfg).run(SAMPLES / name, mode, versions[0], versions[1], notes=_notes_options(args))
            _print_summary(report)
        print(f"outputs written to {cfg.output_dir}")
        return 0
    if args.command == "notes":
        return _show_notes(cfg, args)
    if args.command == "evaluate":
        results = evaluate(cfg, cfg.output_dir)
        print(json.dumps(results, indent=2, default=str))
        print(f"evaluation report written to {cfg.output_dir / 'evaluation_report.md'}")
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
