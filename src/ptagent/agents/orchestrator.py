"""Orchestrator agent: routes a project to Mode A, B or C and coordinates the other agents.

Mode A  no tests exist            -> generate tests, verify, repair
Mode B  tests exist               -> remove obsolete code, find coverage gaps, fill them, verify
Mode C  runtime upgrade requested -> find impacted notebooks/modules and the tests to review
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..kb import KnowledgeBase
from ..llm import build_backend
from .base import Agent, Trace
from .coverage_gap import CoverageGapAgent
from .ingestion import IngestionAgent
from .migration import MigrationAgent
from .migration_plugins import plugin_rules
from .release_notes import ReleaseNotesAgent
from .obsolete_code import ObsoleteCodeAgent
from .runtime_impact import RuntimeImpactAgent
from .test_generator import TestGeneratorAgent
from .verifier import VerifierAgent


class Orchestrator(Agent):
    name = "orchestrator"

    def __init__(self, cfg):
        super().__init__(Trace())
        self.cfg = cfg
        self.backend = build_backend(cfg.llm)
        self.kb = KnowledgeBase.build(cfg.guidelines_path, cfg.dbr_rules_path)
        self.ingestion = IngestionAgent(self.trace, cfg)
        self.generator = TestGeneratorAgent(self.trace, cfg, self.backend, self.kb)
        self.verifier = VerifierAgent(self.trace, cfg)
        self.obsolete = ObsoleteCodeAgent(self.trace, cfg)
        self.gaps = CoverageGapAgent(self.trace)
        self.impact = RuntimeImpactAgent(self.trace, cfg, self.kb)
        self.notes = ReleaseNotesAgent(self.trace, cfg)
        self.migration = MigrationAgent(self.trace, cfg, self.generator, self.verifier, self.backend)

    # ------------------------------------------------------------------ routing
    def run(self, project_dir, mode="auto", from_dbr=None, to_dbr=None, out_root=None,
            apply_in_place=False, confirm=None, notes=None) -> dict:
        index = self.ingestion.run(project_dir)
        out_dir = Path(out_root or self.cfg.output_dir) / index.name
        out_dir.mkdir(parents=True, exist_ok=True)
        self.kb.save(out_dir / "kb_index.json")

        upgrade_requested = bool(from_dbr and to_dbr) or mode == "C"
        if mode == "C":
            resolved = "C"
        elif mode in ("A", "B"):
            resolved = mode
        else:
            resolved = "B" if index.has_tests else "A"
        reason = {
            "A": "no existing tests found" if mode == "auto" else "requested",
            "B": "existing tests found" if mode == "auto" else "requested",
            "C": "runtime upgrade requested",
        }[resolved]
        self.log("routing decision", mode=resolved, reason=reason, runtime_upgrade=upgrade_requested)

        report = {
            "project": index.name,
            "mode_requested": mode,
            "mode_resolved": resolved,
            "llm_backend": self.backend.name,
            "index": index.summary(),
        }
        if resolved == "A":
            report["generation"] = self._generate_and_verify(index, index.functions, out_dir, existing_dirs=[])
        elif resolved == "B":
            report.update(self._mode_b(index, out_dir, apply_in_place, confirm))
        if upgrade_requested:
            frm, to = from_dbr or self.cfg.from_dbr, to_dbr or self.cfg.to_dbr
            rules, brief, advisories, review_extra = self._rules_for(frm, to, out_dir, notes or {})
            report["upgrade_brief"] = brief
            report["release_note_advisories"] = advisories
            report["runtime_impact"] = self.impact.analyze(index, frm, to, rules=rules)
            report["migration"] = self.migration.run(index, report["runtime_impact"], out_dir, rules, review_extra)

        self._write_reports(report, out_dir)
        return report

    # ------------------------------------------------------------------ mode C: rules from the release notes
    def _rules_for(self, frm: str, to: str, out_dir: Path, options: dict):
        """Rules for the upgrade: read from the official release notes, from the curated file, or both."""
        mode = options.get("rules") or self.cfg.rules_mode
        rules, advisories, review_extra, summary = [], [], [], {"mode": mode, "warnings": []}
        if mode in ("dynamic", "both"):
            brief = self.notes.build_brief(frm, to, options.get("local_dir"), bool(options.get("offline")),
                                           bool(options.get("refresh")))
            summary.update(brief.summary())
            if brief.versions:
                rules, advisories, review_extra = self.notes.rules(brief)
            elif mode == "dynamic":
                summary["warnings"].append("Falling back to the curated rule file (written for DBR 15.4 to 17.3), "
                                           "because no release notes could be read.")
                mode = "curated"
        if mode in ("curated", "both"):
            curated = self.impact.load_curated()
            known = {json.dumps(r["matchers"], sort_keys=True) for r in rules}
            rules += [r for r in curated if json.dumps(r["matchers"], sort_keys=True) not in known]
        rules += plugin_rules(rules)
        summary["rules_used"] = len(rules)
        (out_dir / "upgrade_brief.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
        (out_dir / "generated_rules.json").write_text(json.dumps({"rules": rules, "advisories": advisories}, indent=2),
                                                      encoding="utf-8")
        self.log("rules ready", mode=mode, rules=len(rules), advisories=len(advisories))
        return rules, summary, advisories, review_extra

    # ------------------------------------------------------------------ mode B
    def _mode_b(self, index, out_dir, apply_in_place=False, confirm=None) -> dict:
        obsolete = self.obsolete.analyze(index)
        obsolete["cleaned_copies"] = self.obsolete.apply(index, obsolete, out_dir)
        if (out_dir / "cleanup.patch").exists():
            obsolete["patch_file"] = str(out_dir / "cleanup.patch")
        dead = {d["function"] for d in obsolete["dead_functions"]}
        entry_points = {fn.qualname for fn in index.functions if fn.name in self.cfg.entry_point_names}
        existing_dirs = sorted({p.parent for p in index.test_files})

        baseline = self.verifier.run_tests(existing_dirs, index.source_roots, [], out_dir, "baseline", cwd=index.root)
        gaps = self.gaps.analyze(index, baseline if baseline.get("status") == "ran" else None,
                                 exclude=dead | entry_points)
        targets = [fn for fn in index.functions if fn.qualname in set(gaps["targets"])]
        generation = self._generate_and_verify(index, targets, out_dir, existing_dirs=existing_dirs)

        # Measure again on a copy of the project with the obsolete code removed, so coverage is not
        # diluted by code that is about to be deleted.
        cleaned_run = None
        if obsolete["cleaned_copies"]:
            cleaned_root = self._build_cleaned_project(index, out_dir)
            cleaned_existing = [cleaned_root / d.relative_to(index.root) for d in existing_dirs]
            cleaned_roots = [cleaned_root / Path(r).relative_to(index.root) for r in index.source_roots]
            extracted = out_dir / "extracted"
            cleaned_run = self.verifier.run_tests(
                cleaned_existing + [out_dir / "generated_tests"], cleaned_roots,
                [extracted] if extracted.exists() else [], out_dir, "cleaned", cwd=cleaned_root)

        after = generation["execution"].get("coverage_percent")
        after_cleanup = cleaned_run.get("coverage_percent") if cleaned_run and cleaned_run.get("status") == "ran" else None
        warnings = []
        if after_cleanup == 0.0 and (after or 0) > 0:
            warnings.append("The run on the cleaned copy measured 0% although the normal run measured "
                            f"{after}%: the tests probably imported the original sources, so the cleaned "
                            "number was ignored.")
            after_cleanup = None
        final = after_cleanup if after_cleanup is not None else after
        coverage = {
            "baseline_percent": baseline.get("coverage_percent"),
            "after_percent": after,
            "after_cleanup_percent": after_cleanup,
            "final_percent": final,
            "target_percent": self.cfg.coverage_target,
            "verdict": coverage_verdict(final, self.cfg.coverage_target),
        }
        self.log("coverage verdict", **coverage)
        if apply_in_place:
            obsolete["applied_in_place"] = self.obsolete.apply_in_place(
                index, out_dir, confirm or (lambda count: False))
        baseline_slim = {k: v for k, v in baseline.items() if k != "uncovered"}
        if warnings:
            generation["warnings"] = list(generation.get("warnings", [])) + warnings
        report = {"obsolete_code": obsolete, "baseline_run": baseline_slim, "coverage_gaps": gaps,
                  "generation": generation, "coverage": coverage}
        if cleaned_run is not None:
            report["cleaned_run"] = {k: v for k, v in cleaned_run.items() if k != "uncovered"}
        return report

    def _build_cleaned_project(self, index, out_dir: Path) -> Path:
        """Copy the project and overlay the cleaned files, without touching the original."""
        target = out_dir / "cleaned_project"
        if target.exists():
            shutil.rmtree(target)
        skip = set(self.cfg.skip_dirs) | {"outputs", ".venv"}
        out_resolved = out_dir.resolve()

        def ignore(directory, names):
            return [n for n in names if n in skip or n.startswith(".") or n.endswith(".pyc")
                    or (Path(directory) / n).resolve() == out_resolved]

        shutil.copytree(index.root, target, ignore=ignore)
        refactored = out_dir / "refactored"
        for source in refactored.rglob("*"):
            if source.is_file():
                destination = target / source.relative_to(refactored)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        return target

    # ------------------------------------------------------------------ generate + verify + repair
    def _generate_and_verify(self, index, functions, out_dir, existing_dirs) -> dict:
        modules, skipped, retrieval = self.generator.generate(index, functions, out_dir)
        static = self.verifier.static_check(modules)
        extracted = out_dir / "extracted"
        extra = [extracted] if extracted.exists() else []
        paths = list(existing_dirs) + [out_dir / "generated_tests"]
        execution = self.verifier.run_tests(paths, index.source_roots, extra, out_dir, "after", cwd=index.root)
        repairs, attempts, warnings, previous = [], 0, [], None
        while execution.get("status") == "ran" and attempts < self.cfg.max_repair_attempts:
            # Only assertion/call failures can be repaired. Errors (fixture setup, imports) point at the
            # environment or the test file layout, and asking a model to rewrite tests cannot fix them.
            failing = {f["name"]: (f.get("detail") or f["message"]) for f in execution["failures"]
                       if f.get("kind") == "failure" and f["file"].endswith("_generated")}
            if not failing:
                break
            if previous is not None and set(failing) == previous:
                self.log("repair stopped", reason="the same tests are still failing")
                break
            previous = set(failing)
            actions = self.generator.repair(modules, failing, index)
            attempts += 1
            if not actions:
                break
            repairs.extend(actions)
            execution = self.verifier.run_tests(paths, index.source_roots, extra, out_dir, "after", cwd=index.root)
        # Quarantine whatever generated tests still fail. A rerun can reveal failures that were hidden before
        # (for example a test file that only starts collecting once another one is removed), so repeat a few times.
        for _ in range(3):
            if execution.get("status") != "ran":
                break
            leftover = {f["name"]: (f.get("detail") or f["message"]) for f in execution["failures"]
                        if f.get("kind") == "failure" and f["file"].endswith("_generated")}
            if not leftover:
                break
            actions = self.generator.repair(modules, leftover, index, quarantine_only=True)
            repairs.extend(actions)
            if not actions:
                break
            execution = self.verifier.run_tests(paths, index.source_roots, extra, out_dir, "after", cwd=index.root)
        if execution.get("status") == "ran" and execution.get("errors", 0) > 0:
            generated_errors = [f for f in execution["failures"] if f.get("kind") == "error" and f["file"].endswith("_generated")]
            if generated_errors:
                warnings.append(
                    f"{len(generated_errors)} generated tests errored before running (fixture setup or import). "
                    "This usually means the environment, for example Spark could not start. "
                    f"First message: {generated_errors[0]['message']}")
                self.log("environment warning", errors=len(generated_errors))
        generated = [
            {"file": m.path.name, "target_module": m.target_module,
             "functions": [b.function for b in m.blocks],
             "tests": [name for b in m.blocks for name in _names(b.tests)]}
            for m in modules
        ]
        return {
            "functions_targeted": len(functions),
            "modules": generated,
            "tests_generated": sum(len(m["tests"]) for m in generated),
            "skipped": skipped,
            "retrieval": retrieval,
            "static_check": static,
            "execution": {k: v for k, v in execution.items() if k != "uncovered"},
            "repair_actions": repairs,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------ reports
    def _write_reports(self, report: dict, out_dir: Path):
        (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        (out_dir / "run_trace.json").write_text(json.dumps(self.trace.events, indent=2, default=str), encoding="utf-8")
        (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")


def coverage_verdict(final, target) -> str:
    """PASS or FAIL against the coverage target, or 'not measured' when tests were not executed."""
    if final is None:
        return "not measured"
    if final >= target:
        return "PASS"
    return f"FAIL ({round(target - final, 1)} points short)"


def _names(source: str) -> list:
    from .test_generator import test_names
    return test_names(source)


def render_markdown(report: dict) -> str:
    lines = [f"# ptagent report: {report['project']}", "",
             f"- Mode: **{report['mode_resolved']}** (requested: {report['mode_requested']})",
             f"- LLM backend: {report['llm_backend']}",
             f"- Files: {report['index']['source_files']} source, {report['index']['notebooks']} notebooks, "
             f"{report['index']['test_files']} test files, {report['index']['functions']} functions", ""]
    generation = report.get("generation")
    if generation:
        execution = generation["execution"]
        lines += ["## Test generation", "",
                  f"- Functions targeted: {generation['functions_targeted']}",
                  f"- Tests generated: {generation['tests_generated']} in {len(generation['modules'])} files",
                  f"- Skipped functions: {len(generation['skipped'])}"]
        for item in generation["skipped"]:
            lines.append(f"  - `{item['function']}`: {item['reason']}")
        valid = sum(1 for s in generation["static_check"] if s["ok"])
        lines.append(f"- Static check: {valid}/{len(generation['static_check'])} files valid")
        if execution.get("status") == "ran":
            lines.append(f"- Execution: {execution['passed']} passed, {execution['failed']} failed, "
                         f"{execution['errors']} errors; coverage {execution.get('coverage_percent')}%")
        else:
            lines.append(f"- Execution: {execution.get('status')} - {execution.get('reason', '')}")
        lines.append("")
    obsolete = report.get("obsolete_code")
    if obsolete:
        lines += ["## Obsolete code", ""]
        for d in obsolete["dead_functions"]:
            lines.append(f"- Dead function `{d['function']}` ({d['file']}:{d['lines']}), confidence {d.get('confidence', 'high')}")
        for p in obsolete.get("protected_functions", []):
            lines.append(f"- Kept `{p['function']}` ({p['reason']})")
        if obsolete.get("patch_file"):
            lines.append(f"- Cleanup patch: `{obsolete['patch_file']}`")
        for s in obsolete["stale_test_imports"]:
            lines.append(f"- Stale test import `{s['missing_name']}` from `{s['module']}` in {s['test_file']}")
        lines.append("")
    gaps = report.get("coverage_gaps")
    if gaps:
        lines += ["## Coverage gaps", "", f"- Functions without tests: {len(gaps['untested_functions'])}", ""]
    coverage = report.get("coverage")
    if coverage:
        lines += [f"- Coverage: existing tests {coverage['baseline_percent']}%, "
                  f"plus generated tests {coverage['after_percent']}%, "
                  f"after cleanup {coverage.get('after_cleanup_percent')}%",
                  f"- Target {coverage['target_percent']}%: **{coverage.get('verdict')}**", ""]
    brief = report.get("upgrade_brief")
    if brief:
        lines += ["## Release notes read", "", f"- Rules from: {brief.get('mode')}; {brief.get('rules_used', 0)} rules in use"]
        for v in brief.get("versions", []):
            lines.append(f"- DBR {v['version']} ({v['origin']}): Spark {v['spark'] or '?'}, "
                         f"Python {v['environment'].get('python', '?')}, Scala {v['environment'].get('scala', '?')}, "
                         f"{v['behavioral_changes']} behavioral changes")
        for key, change in brief.get("environment_changes", {}).items():
            if change["from"] != change["to"]:
                lines.append(f"- Environment: {key} {change['from']} -> {change['to']}")
        for warning in brief.get("warnings", []):
            lines.append(f"- WARNING: {warning}")
        lines.append("")
    impact = report.get("runtime_impact")
    if impact:
        lines += [f"## Runtime impact (DBR {impact['from_dbr']} to {impact['to_dbr']})", "",
                  f"- Findings: {len(impact['findings'])} in {len(impact['impacted_files'])} files",
                  f"- Tests to review: {len(impact['tests_to_review'])}", ""]
        for f in impact["findings"]:
            lines.append(f"  - {f['rule_id']} {f['file']} {f['location']}: {f['title']}")
        lines.append("")
    migration = report.get("migration")
    if migration:
        lines += ["## Test migration", "", f"- Emulation: {migration['emulation']}",
                  f"- Functions with ANSI-sensitive code: {len(migration['ansi_functions'])}; "
                  f"migrated tests written: {len(migration['migrated_tests'])} "
                  f"in {len(migration['migrated_test_files'])} files"]
        target = migration["existing_tests"]["target_runtime"]
        if target.get("status") == "ran":
            lines.append(f"- Existing tests under the target runtime: {target['passed']} passed, "
                         f"{target['failed']} failed, {target['errors']} errors")
        else:
            lines.append(f"- Existing tests under the target runtime: {target.get('status')} ({target.get('reason', '')})")
        for b in migration["broken_by_upgrade"]:
            lines.append(f"  - broken by the upgrade: `{b['test']}`")
        run = migration.get("migrated_run")
        if run:
            lines.append(f"- Migrated tests: {run.get('status')}"
                         + (f", {run['passed']} passed, {run['failed']} failed, {run['errors']} errors" if run.get("status") == "ran" else ""))
        for item in migration["review_items"]:
            lines.append(f"- Review {item['rule']}: {item['title']} ({', '.join(item['files'])})")
        lines.append("")
    return "\n".join(lines) + "\n"
