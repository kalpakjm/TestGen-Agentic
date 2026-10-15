"""Orchestrator agent: routes a project to Mode A, B or C and coordinates the other agents.

Mode A  no tests exist            -> generate tests, verify, repair
Mode B  tests exist               -> remove obsolete code, find coverage gaps, fill them, verify
Mode C  runtime upgrade requested -> find impacted notebooks/modules and the tests to review
"""
from __future__ import annotations

import json
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
    def run(self, project_dir, mode="auto", from_dbr=None, to_dbr=None, out_root=None) -> dict:
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
            report.update(self._mode_b(index, out_dir))
        if upgrade_requested:
            report["runtime_impact"] = self.impact.analyze(
                index, from_dbr or self.cfg.from_dbr, to_dbr or self.cfg.to_dbr)

        self._write_reports(report, out_dir)
        return report

    # ------------------------------------------------------------------ mode B
    def _mode_b(self, index, out_dir) -> dict:
        obsolete = self.obsolete.analyze(index)
        obsolete["cleaned_copies"] = self.obsolete.apply(index, obsolete, out_dir)
        dead = {d["function"] for d in obsolete["dead_functions"]}
        entry_points = {fn.qualname for fn in index.functions if fn.name in self.cfg.entry_point_names}
        existing_dirs = sorted({p.parent for p in index.test_files})

        baseline = self.verifier.run_tests(existing_dirs, index.source_roots, [], out_dir, "baseline")
        gaps = self.gaps.analyze(index, baseline if baseline.get("status") == "ran" else None,
                                 exclude=dead | entry_points)
        targets = [fn for fn in index.functions if fn.qualname in set(gaps["targets"])]
        generation = self._generate_and_verify(index, targets, out_dir, existing_dirs=existing_dirs)

        coverage = {
            "baseline_percent": baseline.get("coverage_percent"),
            "after_percent": generation["execution"].get("coverage_percent"),
            "target_percent": self.cfg.coverage_target,
        }
        baseline_slim = {k: v for k, v in baseline.items() if k != "uncovered"}
        return {"obsolete_code": obsolete, "baseline_run": baseline_slim, "coverage_gaps": gaps,
                "generation": generation, "coverage": coverage}

    # ------------------------------------------------------------------ generate + verify + repair
    def _generate_and_verify(self, index, functions, out_dir, existing_dirs) -> dict:
        modules, skipped, retrieval = self.generator.generate(index, functions, out_dir)
        static = self.verifier.static_check(modules)
        extracted = out_dir / "extracted"
        extra = [extracted] if extracted.exists() else []
        paths = list(existing_dirs) + [out_dir / "generated_tests"]
        execution = self.verifier.run_tests(paths, index.source_roots, extra, out_dir, "after")
        repairs, attempts = [], 0
        while execution.get("status") == "ran" and attempts < self.cfg.max_repair_attempts:
            failing = {f["name"]: f["message"] for f in execution["failures"] if f["file"].endswith("_generated")}
            if not failing:
                break
            actions = self.generator.repair(modules, failing, index)
            attempts += 1
            if not actions:
                break
            repairs.extend(actions)
            execution = self.verifier.run_tests(paths, index.source_roots, extra, out_dir, "after")
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
        }

    # ------------------------------------------------------------------ reports
    def _write_reports(self, report: dict, out_dir: Path):
        (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        (out_dir / "run_trace.json").write_text(json.dumps(self.trace.events, indent=2, default=str), encoding="utf-8")
        (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")


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
            lines.append(f"- Dead function `{d['function']}` ({d['file']}:{d['lines']})")
        for s in obsolete["stale_test_imports"]:
            lines.append(f"- Stale test import `{s['missing_name']}` from `{s['module']}` in {s['test_file']}")
        lines.append("")
    gaps = report.get("coverage_gaps")
    if gaps:
        lines += ["## Coverage gaps", "", f"- Functions without tests: {len(gaps['untested_functions'])}", ""]
    coverage = report.get("coverage")
    if coverage:
        lines += [f"- Coverage before: {coverage['baseline_percent']}, after: {coverage['after_percent']}, "
                  f"target: {coverage['target_percent']}", ""]
    impact = report.get("runtime_impact")
    if impact:
        lines += [f"## Runtime impact (DBR {impact['from_dbr']} to {impact['to_dbr']})", "",
                  f"- Findings: {len(impact['findings'])} in {len(impact['impacted_files'])} files",
                  f"- Tests to review: {len(impact['tests_to_review'])}", ""]
        for f in impact["findings"]:
            lines.append(f"  - {f['rule_id']} {f['file']} {f['location']}: {f['title']}")
        lines.append("")
    return "\n".join(lines) + "\n"
