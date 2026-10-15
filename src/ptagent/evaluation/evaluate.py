"""Initial (baseline) evaluation on the bundled, hand-labelled sample projects.

Labels live in data/labels/*.json and were written by hand from the sample code, independently of the
agents. The sample set is small on purpose: this is a sanity check of the baseline, not a benchmark.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..agents.orchestrator import Orchestrator
from ..config import REPO_ROOT
from .metrics import precision_recall

SAMPLES = REPO_ROOT / "data" / "sample_projects"
LABELS = REPO_ROOT / "data" / "labels"


def _labels(name: str) -> dict:
    return json.loads((LABELS / f"{name}.json").read_text(encoding="utf-8"))


def evaluate(cfg, out_root: Path) -> dict:
    out_root = Path(out_root)
    results = {"llm_backend": cfg.llm.backend}

    # Mode A: which functions get tests, and are the generated files valid?
    labels = _labels("orders_no_tests")
    report = Orchestrator(cfg).run(SAMPLES / "orders_no_tests", mode="A", out_root=out_root)
    generation = report["generation"]
    targeted = {f"{m['target_module']}.{fn}" for m in generation["modules"] for fn in m["functions"]}
    results["mode_a_generation"] = {
        "eligibility": precision_recall(targeted, set(labels["expected_tests_for"])),
        "tests_generated": generation["tests_generated"],
        "files_syntactically_valid": f"{sum(s['ok'] for s in generation['static_check'])}/{len(generation['static_check'])}",
        "skipped_as_expected": sorted(x["function"] for x in generation["skipped"]) == sorted(labels["expected_skipped"]),
        "execution": _execution_summary(generation["execution"]),
    }

    # Mode B: dead code, stale tests, untested functions
    labels = _labels("orders_with_tests")
    report = Orchestrator(cfg).run(SAMPLES / "orders_with_tests", mode="B", out_root=out_root)
    obsolete = report["obsolete_code"]
    results["mode_b_maintenance"] = {
        "dead_functions": precision_recall({d["function"] for d in obsolete["dead_functions"]}, set(labels["dead_functions"])),
        "stale_test_imports": precision_recall(
            {(s["test_file"], s["missing_name"]) for s in obsolete["stale_test_imports"]},
            {tuple(x) for x in labels["stale_test_imports"]}),
        "untested_functions": precision_recall(
            set(report["coverage_gaps"]["untested_functions"]), set(labels["untested_functions"])),
        "coverage": report["coverage"],
        "execution": _execution_summary(report["generation"]["execution"]),
    }

    # Mode C: runtime impact detection
    labels = _labels("legacy_runtime")
    report = Orchestrator(cfg).run(SAMPLES / "legacy_runtime", mode="C", from_dbr="13.3", to_dbr="17.3", out_root=out_root)
    found = {(f["file"], f["rule_id"], f["location"]) for f in report["runtime_impact"]["findings"]}
    results["mode_c_runtime_impact"] = {
        "findings": precision_recall(found, {tuple(x) for x in labels["findings"]}),
        "impacted_files": precision_recall(set(report["runtime_impact"]["impacted_files"]), set(labels["impacted_files"])),
    }

    (out_root / "evaluation_report.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    (out_root / "evaluation_report.md").write_text(render(results), encoding="utf-8")
    return results


def _execution_summary(execution: dict) -> dict:
    if execution.get("status") != "ran":
        return {"status": execution.get("status"), "reason": execution.get("reason")}
    return {k: execution.get(k) for k in ("status", "total", "passed", "failed", "errors", "coverage_percent")}


def render(results: dict) -> str:
    a, b, c = results["mode_a_generation"], results["mode_b_maintenance"], results["mode_c_runtime_impact"]
    lines = ["# Baseline evaluation", "", f"LLM backend: {results['llm_backend']}", "",
             "## Mode A: test generation (orders_no_tests)", "",
             f"- Eligibility precision/recall: {a['eligibility']['precision']} / {a['eligibility']['recall']}",
             f"- Tests generated: {a['tests_generated']}; files valid: {a['files_syntactically_valid']}",
             f"- Skipped functions as expected: {a['skipped_as_expected']}",
             f"- Execution: {a['execution']}", "",
             "## Mode B: maintenance (orders_with_tests)", "",
             f"- Dead functions P/R: {b['dead_functions']['precision']} / {b['dead_functions']['recall']}",
             f"- Stale test imports P/R: {b['stale_test_imports']['precision']} / {b['stale_test_imports']['recall']}",
             f"- Untested functions P/R: {b['untested_functions']['precision']} / {b['untested_functions']['recall']}",
             f"- Coverage: {b['coverage']}",
             f"- Execution: {b['execution']}", "",
             "## Mode C: runtime impact (legacy_runtime, DBR 13.3 to 17.3)", "",
             f"- Findings P/R: {c['findings']['precision']} / {c['findings']['recall']}",
             f"- Impacted files P/R: {c['impacted_files']['precision']} / {c['impacted_files']['recall']}", ""]
    return "\n".join(lines)
