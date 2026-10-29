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

    # ------------------------------------------------------------------ routing
    def run(self, project_dir, mode="auto", from_dbr=None, to_dbr=None, out_root=None,
            apply_in_place=False, confirm=None) -> dict:
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
            report["runtime_impact"] = self.impact.analyze(
                index, from_dbr or self.cfg.from_dbr, to_dbr or self.cfg.to_dbr)

        self._write_reports(report, out_dir)
        return report

    # ------------------------------------------------------------------ mode B
    def _mode_b(self, index, out_dir, apply_in_place=False, confirm=None) -> dict:
        obsolete = self.obsolete.analyze(index)
        obsolete["cleaned_copies"] = self.obsolete.apply(index, obsolete, out_dir)
        if (out_dir / "cleanup.patch").exists():
            obsolete["patch_file"] = str(out_dir / "cleanup.patch")
        dead = {d["function"] for d in obsolete["dead_functions"]}
        entry_points = {fn.qualname for fn in index.functions if fn.name in self.cfg.entry_point_names}
        existing_dirs = sorted({p.parent for p in index.test_files})

        baseline = self.verifier.run_tests(existing_dirs, index.source_roots, [], out_dir, "baseline")
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
                [extracted] if extracted.exists() else [], out_dir, "cleaned")

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
        execution = self.verifier.run_tests(paths, index.source_roots, extra, out_dir, "after")
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
            execution = self.verifier.run_tests(paths, index.source_roots, extra, out_dir, "after")
        if execution.get("status") == "ran":
            leftover = {f["name"]: (f.get("detail") or f["message"]) for f in execution["failures"]
                        if f.get("kind") == "failure" and f["file"].endswith("_generated")}
            if leftover:
                actions = self.generator.repair(modules, leftover, index, quarantine_only=True)
                repairs.extend(actions)
                execution = self.verifier.run_tests(paths, index.source_roots, extra, out_dir, "after")
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
    impact = report.get("runtime_impact")
    if impact:
        lines += [f"## Runtime impact (DBR {impact['from_dbr']} to {impact['to_dbr']})", "",
                  f"- Findings: {len(impact['findings'])} in {len(impact['impacted_files'])} files",
                  f"- Tests to review: {len(impact['tests_to_review'])}", ""]
        for f in impact["findings"]:
            lines.append(f"  - {f['rule_id']} {f['file']} {f['location']}: {f['title']}")
        lines.append("")
    return "\n".join(lines) + "\n"
