"""Migration agent (Mode C): turns the runtime-impact findings into executable test changes.

What it does in the baseline:
  * runs the project's existing tests under an emulation of the target runtime's ANSI default and
    reports the tests that pass today but fail there (the tests the upgrade breaks);
  * writes new tests for every function with an ANSI-sensitive cast or date parse: one asserts the
    new behaviour (bad input raises) and one documents the old behaviour (bad input becomes null);
    both set spark.sql.ansi.enabled themselves, so they give the same result on any runtime;
  * lists every other finding as a review item, because it cannot be tested on a local PySpark;
  * with an LLM backend, proposes rewrites of the broken tests (never executed, marked unverified).

Code migration is an optional add-on and is not done here.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from string import Template

from ..llm import GenerationRequest
from ..tools.analyzer import notebook_extract_source
from ..tools.columns import SAMPLE_VALUES
from .base import Agent
from .test_generator import SPARK_HELPERS

INVALID_VALUES = {"numeric_string": "not-a-number", "date": "not-a-date"}


def emulation_env(confs: dict) -> dict:
    """Environment variables that start Spark with the given options (used to emulate a runtime)."""
    args = " ".join(f"--conf {key}={value}" for key, value in sorted(confs.items()))
    env = {"PYSPARK_SUBMIT_ARGS": f"{args} pyspark-shell", "PTAGENT_SPARK_CONF": json.dumps(confs)}
    if "spark.sql.ansi.enabled" in confs:
        env["PTAGENT_ANSI"] = confs["spark.sql.ansi.enabled"]
    return env


def emulation_confs(rules_checked: list):
    """Options whose default changes in the upgrade: (target values, values before the upgrade)."""
    target, current = {}, {}
    for rule in rules_checked:
        emulation = rule.get("emulation")
        if emulation and emulation["value"] in ("true", "false"):
            target[emulation["conf"]] = emulation["value"]
            current[emulation["conf"]] = "false" if emulation["value"] == "true" else "true"
    return target, current


class MigrationAgent(Agent):
    name = "migration"

    def __init__(self, trace, cfg, generator, verifier, backend):
        super().__init__(trace)
        self.cfg = cfg
        self.generator = generator
        self.verifier = verifier
        self.backend = backend
        path = Path(cfg.prompts_dir) / "runtime_migration.txt"
        self.prompt = Template(path.read_text(encoding="utf-8")) if path.exists() else None

    # ------------------------------------------------------------------ main entry
    def run(self, index, impact: dict, out_dir: Path, rules=None, extra_review=None) -> dict:
        out_dir = Path(out_dir)
        rules = rules or []
        rules_by_id = {r["id"]: r for r in impact["rules_checked"]}
        ansi_ids = {r["id"] for r in rules if r.get("plugin") == "ansi"}
        ansi_findings = [f for f in impact["findings"] if f["rule_id"] in ansi_ids]
        functions = self._functions_for(index, ansi_findings)
        target_confs, current_confs = emulation_confs(impact["rules_checked"])
        result = {
            "emulation": ("Options set to their new defaults on the local PySpark: "
                          + ", ".join(f"{k}={v}" for k, v in sorted(target_confs.items()))) if target_confs
                         else "No option with a changed default was found in the release notes, so nothing is emulated.",
            "emulated_options": target_confs,
            "ansi_functions": [fn.qualname for fn in functions],
        }
        migrated_dir = out_dir / "migrated_tests"
        modules, skipped = self._write_tests(index, functions, migrated_dir, out_dir)
        result["migrated_functions"] = [fn for m in modules for fn in m["functions"]]
        result["migrated_test_files"] = [m["file"] for m in modules]
        result["migrated_tests"] = [t for m in modules for t in m["tests"]]
        result["skipped_functions"] = skipped

        existing_dirs = sorted({p.parent for p in index.test_files})
        extra = [out_dir / "extracted"] if (out_dir / "extracted").exists() else []
        if target_confs:
            current = self.verifier.run_tests(existing_dirs, index.source_roots, [], out_dir, "current_runtime", emulation_env(current_confs))
            target = self.verifier.run_tests(existing_dirs, index.source_roots, [], out_dir, "target_runtime", emulation_env(target_confs))
        else:
            current = target = {"status": "skipped", "reason": "no option with a changed default to emulate"}
        result["existing_tests"] = {"current_runtime": self._slim(current), "target_runtime": self._slim(target)}
        broken = []
        if current.get("status") == "ran" and target.get("status") == "ran":
            failing_now = {f["name"] for f in current["failures"]}
            broken = [f for f in target["failures"] if f["name"] not in failing_now]
        result["broken_by_upgrade"] = [{"test": f["name"], "message": f["message"]} for f in broken]

        if modules:
            run = self.verifier.run_tests([migrated_dir], index.source_roots, extra, out_dir, "migrated",
                                          emulation_env(target_confs) if target_confs else None)
            result["migrated_run"] = self._slim(run)
        ansi_rule = next((rules_by_id[i] for i in ansi_ids if i in rules_by_id), {})
        result["proposals"] = self._propose(index, broken, {"R002": ansi_rule}, impact, migrated_dir)
        result["review_items"] = self._review_items(index, impact, result["migrated_functions"], ansi_ids) + list(extra_review or [])
        self.log("migration", migrated_functions=len(result["migrated_functions"]),
                 broken_tests=len(broken), review_items=len(result["review_items"]))
        self._unload()
        return result

    @staticmethod
    def _slim(run: dict) -> dict:
        return {k: v for k, v in run.items() if k != "uncovered"}

    def _unload(self):
        unload = getattr(self.backend, "unload", None)
        if unload:
            unload()

    # ------------------------------------------------------------------ which functions
    @staticmethod
    def _functions_for(index, findings) -> list:
        found = []
        for finding in findings:
            location = finding["location"]
            cell, line = None, 0
            if location.startswith("cell"):
                head, _, tail = location.partition(":L")
                cell, line = int(head[4:]), int(tail)
            else:
                line = int(location[1:])
            for fn in index.functions:
                if fn.file != finding["file"] or not (fn.lineno <= line <= fn.end_lineno):
                    continue
                if fn.cell == cell and fn not in found:
                    found.append(fn)
        return found

    # ------------------------------------------------------------------ test writing
    def _write_tests(self, index, functions, migrated_dir: Path, out_dir: Path):
        skipped, by_module = [], {}
        for fn in functions:
            plan, reason = self.generator.plan(fn)
            if plan is None or plan["kind"] != "spark":
                skipped.append({"function": fn.qualname, "reason": reason or "needs a DataFrame input to test"})
                continue
            by_module.setdefault(fn.module, []).append((fn, plan))
        modules, extra_paths = [], []
        for module, items in sorted(by_module.items()):
            if items[0][0].origin == "notebook":
                extracted = out_dir / "extracted"
                extracted.mkdir(parents=True, exist_ok=True)
                (extracted / f"{module}.py").write_text(notebook_extract_source(index, items[0][0].file), encoding="utf-8")
                extra_paths.append(extracted)
            names = ", ".join(sorted(fn.name for fn, _ in items))
            header = (f'"""Tests for the ANSI-mode change in the target runtime (written by ptagent).\n\nTarget module: {module}\n"""\n'
                      "import pytest  # noqa: F401\nfrom pyspark.sql import DataFrame, Row, functions as F, types as T  # noqa: F401\n\n"
                      f"from {module} import {names}\n\n\n{SPARK_HELPERS}")
            body, tests = [], []
            for fn, plan in items:
                constants = self.generator._constants(fn, plan) + self._bad_rows(fn, plan)
                text = self._template(fn, plan)
                body.append(constants.rstrip() + "\n\n\n" + text.rstrip() + "\n")
                tests += [n for n in self._names(text)]
            migrated_dir.mkdir(parents=True, exist_ok=True)
            path = migrated_dir / f"test_{module.replace('.', '_')}_ansi_migration.py"
            path.write_text(header + "\n\n" + "\n\n".join(body), encoding="utf-8")
            modules.append({"file": path.name, "module": module, "functions": [fn.qualname for fn, _ in items], "tests": tests})
        if modules:
            self.generator.write_conftest(migrated_dir, [Path(r) for r in index.source_roots] + extra_paths)
        return modules, skipped

    @staticmethod
    def _names(source: str) -> list:
        return [n.name for n in ast.parse(source).body if isinstance(n, ast.FunctionDef)]

    @staticmethod
    def _bad_rows(fn, plan) -> str:
        """One valid row and one row whose cast or date columns hold text that cannot be parsed."""
        cols = sorted(plan["columns"].items())
        valid = tuple(SAMPLE_VALUES[kind][0] for _, kind in cols)
        invalid = tuple(INVALID_VALUES.get(kind, SAMPLE_VALUES[kind][1]) for _, kind in cols)
        return f"_BAD_ROWS_{fn.name.upper()} = {[valid, invalid]!r}\n"

    @staticmethod
    def _template(fn, plan) -> str:
        key, name = fn.name.upper(), fn.name
        build = "\n        ".join(f"{v} = _make(spark, _SCHEMA_{key}, _BAD_ROWS_{key})" for v in plan["df_vars"])
        call = f"{name}({plan['call_args']})"
        return f'''def test_{name}_bad_input_raises_when_ansi_is_on(spark):
    """Runtimes 17.0 and above: a value that cannot be cast or parsed raises instead of becoming null."""
    previous = spark.conf.get("spark.sql.ansi.enabled")
    spark.conf.set("spark.sql.ansi.enabled", "true")
    try:
        {build}
        with pytest.raises(Exception):
            _materialize({call})
    finally:
        spark.conf.set("spark.sql.ansi.enabled", previous)


def test_{name}_bad_input_becomes_null_when_ansi_is_off(spark):
    """Before 17.0 (and with spark.sql.ansi.enabled=false): the same input does not raise."""
    previous = spark.conf.get("spark.sql.ansi.enabled")
    spark.conf.set("spark.sql.ansi.enabled", "false")
    try:
        {build}
        result = _materialize({call})
        assert isinstance(result, DataFrame)
    finally:
        spark.conf.set("spark.sql.ansi.enabled", previous)
'''

    # ------------------------------------------------------------------ LLM proposals and review items
    def _test_source(self, index, test_name: str):
        for path in index.test_files:
            text = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            lines = text.splitlines()
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == test_name:
                    return "\n".join(lines[node.lineno - 1 : node.end_lineno])
        return None

    def _propose(self, index, broken, rules, impact, migrated_dir: Path) -> list:
        """Ask the model for a rewrite of each broken test. Proposals are saved but never executed."""
        proposals = []
        if not broken or self.prompt is None or not getattr(self.backend, "uses_llm", False):
            return proposals
        rule = rules.get("R002", {})
        code = "\n".join(f["snippet"] for f in impact["findings"] if f["rule_id"] == "R002")[:1200]
        for item in broken:
            source = self._test_source(index, item["name"])
            if not source:
                continue
            prompt = self.prompt.safe_substitute(
                from_dbr=impact["from_dbr"], to_dbr=impact["to_dbr"], rule_id="R002",
                rule_description=rule.get("title", "ANSI SQL mode on by default"), code=code,
                tests=source, failure=item["message"])
            reply = self.backend.generate(GenerationRequest("migration", prompt, lambda: ""))
            if not reply.used_llm:
                continue
            try:
                ast.parse(reply.text)
            except SyntaxError:
                continue
            target = Path(migrated_dir) / "proposals"
            target.mkdir(parents=True, exist_ok=True)
            path = target / f"{item['name']}.py"
            path.write_text("# UNVERIFIED proposal written by a language model. Review it and run it before use.\n"
                            + reply.text.rstrip() + "\n", encoding="utf-8")
            proposals.append({"test": item["name"], "file": str(path)})
        return proposals

    def _review_items(self, index, impact: dict, migrated_functions: list, ansi_ids: set = frozenset()) -> list:
        """Findings that cannot be turned into a local test, grouped by rule."""
        items = []
        migrated = set(migrated_functions)
        for action in impact["actions"]:
            files, note = action["files"], "Not testable on a local PySpark; check on a cluster with the target runtime."
            if action["rule_id"] in ansi_ids:
                # keep only the ANSI findings that sit outside the functions that received migrated tests
                leftover = []
                for finding in impact["findings"]:
                    if finding["rule_id"] not in ansi_ids:
                        continue
                    owners = self._functions_for(index, [finding])
                    if not owners or owners[0].qualname not in migrated:
                        leftover.append(finding["file"])
                if not leftover:
                    continue
                files = sorted(set(leftover))
                note = "ANSI-sensitive code outside the functions that got migrated tests (module-level or notebook code)."
            items.append({"rule": action["rule_id"], "title": action["title"], "files": files,
                          "action": action["recommendation"], "note": note})
        return items
