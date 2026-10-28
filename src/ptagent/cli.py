"""Command line interface: python run.py <command> ..."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agents.orchestrator import Orchestrator
from .config import REPO_ROOT, load_config
from .evaluation.evaluate import evaluate
from .tools.analyzer import build_index

SAMPLES = REPO_ROOT / "data" / "sample_projects"


def _config(args):
    cfg = load_config(Path(args.config) if args.config else None)
    if getattr(args, "backend", None):
        cfg.llm.backend = args.backend
    if getattr(args, "output_dir", None):
        cfg.output_dir = Path(args.output_dir).resolve()
    return cfg


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run.py", description="Multi-agent test generation for PySpark on Databricks")
    parser.add_argument("--config", help="path to a config YAML (default: configs/config.yaml)")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="index a project and print a summary")
    analyze.add_argument("project")

    run = sub.add_parser("run", help="run the agents on a project")
    run.add_argument("project")
    run.add_argument("--mode", choices=["auto", "A", "B", "C"], default="auto")
    run.add_argument("--from-dbr", help="current Databricks Runtime, e.g. 13.3")
    run.add_argument("--to-dbr", help="target Databricks Runtime, e.g. 17.3")
    run.add_argument("--backend", choices=["offline", "ollama"])
    run.add_argument("--output-dir")

    demo = sub.add_parser("demo", help="run all three modes on the bundled sample projects")
    demo.add_argument("--backend", choices=["offline", "ollama"])
    demo.add_argument("--output-dir")

    ev = sub.add_parser("evaluate", help="run the baseline evaluation on the labelled samples")
    ev.add_argument("--backend", choices=["offline", "ollama"])
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
    if report.get("runtime_impact"):
        r = report["runtime_impact"]
        print(f"  runtime impact DBR {r['from_dbr']} -> {r['to_dbr']}: {len(r['findings'])} findings in "
              f"{len(r['impacted_files'])} files; {len(r['tests_to_review'])} test files to review")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = _config(args)
    if args.command == "analyze":
        print(json.dumps(build_index(args.project, cfg.skip_dirs).summary(), indent=2))
        return 0
    if args.command == "run":
        report = Orchestrator(cfg).run(Path(args.project), args.mode, args.from_dbr, args.to_dbr)
        _print_summary(report)
        print(f"  outputs: {cfg.output_dir / report['project']}")
        return 0
    if args.command == "demo":
        for name, mode, versions in (("orders_no_tests", "A", (None, None)),
                                     ("orders_with_tests", "B", (None, None)),
                                     ("legacy_runtime", "C", ("13.3", "17.3"))):
            report = Orchestrator(cfg).run(SAMPLES / name, mode, versions[0], versions[1])
            _print_summary(report)
        print(f"outputs written to {cfg.output_dir}")
        return 0
    if args.command == "evaluate":
        results = evaluate(cfg, cfg.output_dir)
        print(json.dumps(results, indent=2, default=str))
        print(f"evaluation report written to {cfg.output_dir / 'evaluation_report.md'}")
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
