"""Test generation agent (Mode A, and gap filling in Mode B).

For every eligible function it prepares context (inferred schema + retrieved guidance), asks the
LLM backend for pytest tests, validates the answer and falls back to a deterministic template when
the answer is unusable. With the default offline backend the template is always used.
"""
from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from string import Template

from ..llm import GenerationRequest
from ..models import FunctionTests, GeneratedModule
from ..tools.analyzer import notebook_extract_source
from ..tools.columns import SAMPLE_VALUES, infer_columns
from .base import Agent

PRIMITIVE_SAMPLES = {"str": '"a"', "int": "1", "float": "1.0", "bool": "True"}
PATHISH = re.compile(r"(path|file|dir|uri|url)", re.I)
SPARK_TYPES = {
    "string": "T.StringType()",
    "numeric_string": "T.StringType()",
    "date": "T.StringType()",
    "numeric": "T.DoubleType()",
}

CONFTEST = '''"""Shared fixtures for ptagent generated tests."""
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
for _relative in __PATHS__:
    _candidate = str((_HERE / _relative).resolve())
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)
for _extra in os.environ.get("PTAGENT_PYTHONPATH", "").split(os.pathsep):
    if _extra and _extra not in sys.path:
        sys.path.insert(0, _extra)


@pytest.fixture(scope="session")
def spark():
    """Small local SparkSession shared by all generated tests."""
    from pyspark.sql import SparkSession

    return (
        SparkSession.builder.master("local[1]")
        .appName("ptagent-generated-tests")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", "1")
        .config("spark.sql.session.timeZone", "UTC")
        .getOrCreate()
    )
'''

SPARK_HELPERS = '''def _make(spark, schema, rows):
    """Build a DataFrame with an explicit schema (no inference)."""
    return spark.createDataFrame(rows, schema=schema)


def _materialize(result):
    """Force execution of lazy DataFrames so errors surface inside the test."""
    if isinstance(result, DataFrame):
        result.collect()
    return result
'''


def remove_functions(source: str, names) -> str:
    """Remove top-level functions (with decorators) by name, using AST line ranges."""
    tree = ast.parse(source)
    lines = source.splitlines()
    drop = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            drop.update(range(start, node.end_lineno + 1))
    kept = [line for i, line in enumerate(lines, start=1) if i not in drop]
    return "\n".join(kept).strip("\n") + "\n" if kept else ""


def test_names(source: str) -> list:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [n.name for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test")]


class TestGeneratorAgent(Agent):
    name = "test_generator"
    __test__ = False  # not a pytest test class

    def __init__(self, trace, cfg, backend, kb):
        super().__init__(trace)
        self.cfg = cfg
        self.backend = backend
        self.kb = kb
        self.generation_prompt = Template((cfg.prompts_dir / "test_generation.txt").read_text(encoding="utf-8"))
        self.repair_prompt = Template((cfg.prompts_dir / "test_repair.txt").read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ planning
    def plan(self, fn):
        """Return (plan, None) when tests can be generated, otherwise (None, reason)."""
        if fn.name.startswith("_"):
            return None, "private function"
        if fn.name.startswith("test"):
            return None, "test function"
        if fn.name in self.cfg.entry_point_names:
            return None, "entry point (needs an integration test)"
        args, kwargs, df_vars, spark = [], [], [], False
        numeric_params = []
        for p in fn.params:
            if p.kind in {"vararg", "kwarg"}:
                continue
            annotation = (p.annotation or "").strip()
            base = annotation.split("[")[0]
            if base in {"int", "float"}:
                numeric_params.append(p.name)
            if p.name == "spark" or annotation.endswith("SparkSession"):
                value, spark = "spark", True
            elif annotation.endswith("DataFrame") or p.name in self.cfg.dataframe_param_names:
                value = "df" if not df_vars else f"df_{len(df_vars) + 1}"
                df_vars.append(value)
            elif p.has_default:
                continue
            elif PATHISH.search(p.name):
                return None, f"parameter '{p.name}' needs a file fixture (planned for the final submission)"
            elif base in PRIMITIVE_SAMPLES:
                value = PRIMITIVE_SAMPLES[base]
            else:
                shown = annotation or "unannotated"
                return None, f"parameter '{p.name}' has unsupported type ({shown})"
            (kwargs if p.kind == "kwonly" else args).append(value if p.kind != "kwonly" else f"{p.name}={value}")
        kind = "spark" if df_vars else ("spark_only" if spark else "pure")
        inference = infer_columns(fn.source, numeric_params) if df_vars else None
        return {
            "kind": kind,
            "call_args": ", ".join(args + kwargs),
            "df_vars": df_vars,
            "columns": inference.columns if inference else {},
            "returns": fn.returns,
        }, None

    # ------------------------------------------------------------------ rendering
    @staticmethod
    def _constants(fn, plan) -> str:
        if plan["kind"] != "spark":
            return ""
        key = fn.name.upper()
        cols = sorted(plan["columns"].items())
        fields = ", ".join(f'T.StructField("{name}", {SPARK_TYPES[kind]}, True)' for name, kind in cols)
        rows = [tuple(SAMPLE_VALUES[kind][i] for _, kind in cols) for i in range(2)]
        return (
            f"_SCHEMA_{key} = T.StructType([{fields}])\n"
            f"_ROWS_{key} = {rows!r}\n"
            f"_NULL_ROWS_{key} = [(None,) * {len(cols)}, (None,) * {len(cols)}]\n"
        )

    @staticmethod
    def _asserts(returns):
        base = (returns or "").split("[")[0].strip()
        if base.endswith("DataFrame"):
            return ["assert isinstance(result, DataFrame)"]
        if base in {"str", "int", "float", "bool"}:
            return [f"assert isinstance(result, {base})"]
        if base == "None":
            return ["assert result is None"]
        return ["# return type is not annotated: this test checks that the call succeeds"]

    def _template(self, fn, plan) -> str:
        name, key = fn.name, fn.name.upper()
        call = f"{name}({plan['call_args']})"
        asserts = "\n    ".join(self._asserts(plan["returns"]))
        if plan["kind"] == "pure":
            return (
                f"def test_{name}_returns_expected_type():\n    result = {call}\n    {asserts}\n\n\n"
                f"def test_{name}_is_deterministic():\n    assert {call} == {call}\n"
            )
        if plan["kind"] == "spark_only":
            return f"def test_{name}_returns_expected_type(spark):\n    result = _materialize({call})\n    {asserts}\n"

        def build(rows_name):
            return "\n    ".join(f"{v} = _make(spark, _SCHEMA_{key}, {rows_name})" for v in plan["df_vars"])

        blocks = []
        for suffix, rows_name in (
            ("returns_expected_type", f"_ROWS_{key}"),
            ("handles_empty_input", "[]"),
            ("handles_null_values", f"_NULL_ROWS_{key}"),
        ):
            blocks.append(
                f"def test_{name}_{suffix}(spark):\n    {build(rows_name)}\n"
                f"    result = _materialize({call})\n    {asserts}\n"
            )
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------ generation
    def _valid(self, text: str, fn) -> bool:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return False
        if any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in ast.walk(tree)):
            return False
        names = [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]
        return bool(names) and all(n.startswith(f"test_{fn.name}") for n in names)

    def _generate_block(self, fn, plan):
        constants = self._constants(fn, plan)
        template_text = self._template(fn, plan)
        context_chunks = self.kb.search(self._query(fn, plan), k=2) if self.kb else []
        context = "\n".join(f"- {c.title}: {c.text}" for c in context_chunks) or "(none)"
        prompt = self.generation_prompt.safe_substitute(
            module=fn.module,
            function_source=fn.source,
            function_name=fn.name,
            columns=json.dumps(plan["columns"]) if plan["columns"] else "(not a DataFrame function)",
            constants=", ".join(re.findall(r"^(_\w+) =", constants, re.M)) or "(none)",
            context=context,
        )
        result = self.backend.generate(GenerationRequest("test_generation", prompt, lambda: template_text))
        text, used_llm, note = result.text, result.used_llm, result.note
        if used_llm and not self._valid(text, fn):
            text, used_llm, note = template_text, False, "LLM output rejected by validation; used template"
        return FunctionTests(fn.name, constants, text, used_llm, note), [c.id for c in context_chunks]

    @staticmethod
    def _query(fn, plan) -> str:
        tokens = re.findall(r"[A-Za-z_]+", fn.source)
        return " ".join(tokens) + " null empty dataframe test"

    def generate(self, index, functions, out_dir: Path):
        """Generate test modules for the given functions. Returns (modules, skipped, retrieval)."""
        out_dir = Path(out_dir)
        tests_dir = out_dir / "generated_tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        by_module, skipped, retrieval = {}, [], {}
        for fn in functions:
            plan, reason = self.plan(fn)
            if plan is None:
                skipped.append({"function": fn.qualname, "reason": reason})
                continue
            by_module.setdefault(fn.module, []).append((fn, plan))

        path_entries = []
        modules = []
        for module, items in sorted(by_module.items()):
            origin = items[0][0]
            if origin.origin == "notebook":
                extracted = out_dir / "extracted"
                extracted.mkdir(parents=True, exist_ok=True)
                (extracted / f"{module}.py").write_text(notebook_extract_source(index, origin.file), encoding="utf-8")
                path_entries.append(extracted)
            blocks = []
            for fn, plan in items:
                block, chunk_ids = self._generate_block(fn, plan)
                blocks.append(block)
                retrieval[fn.qualname] = chunk_ids
            names = ", ".join(sorted(fn.name for fn, _ in items))
            uses_spark = any(plan["kind"] != "pure" for _, plan in items)
            header = f'"""Auto-generated by ptagent ({self.backend.name}). Review before committing.\n\nTarget module: {module}\n"""\nimport pytest  # noqa: F401\n'
            if uses_spark:
                header += "from pyspark.sql import DataFrame, types as T  # noqa: F401\n"
            header += f"\nfrom {module} import {names}\n"
            if any(plan["kind"] == "spark" or plan["kind"] == "spark_only" for _, plan in items):
                header += "\n\n" + SPARK_HELPERS
            path = tests_dir / f"test_{module.replace('.', '_')}_generated.py"
            generated = GeneratedModule(module, path, header, blocks)
            path.write_text(generated.render(), encoding="utf-8")
            modules.append(generated)
            self.log("generated tests", module=module, functions=[fn.name for fn, _ in items],
                     backend=self.backend.name, llm_used=any(b.used_llm for b in blocks))

        roots = [Path(r) for r in index.source_roots] + path_entries
        relative = sorted({os.path.relpath(r, tests_dir) for r in roots})
        (tests_dir / "conftest.py").write_text(CONFTEST.replace("__PATHS__", repr(relative)), encoding="utf-8")
        for item in skipped:
            self.log("skipped function", **item)
        return modules, skipped, retrieval

    # ------------------------------------------------------------------ repair
    def repair(self, modules, failures, index):
        """Fix or quarantine failing generated tests. failures: {test name: message}. Returns actions."""
        actions = []
        for module in modules:
            for block in list(module.blocks):
                names = test_names(block.tests)
                failing = [n for n in names if n in failures]
                if not failing:
                    continue
                fn = next((f for f in index.functions if f.name == block.function and f.module == module.target_module), None)
                new_text = None
                if fn is not None and self.backend.name == "ollama":
                    prompt = self.repair_prompt.safe_substitute(
                        function_name=block.function, tests=block.tests,
                        failures="\n".join(f"{n}: {failures[n]}" for n in failing),
                        function_source=fn.source)
                    result = self.backend.generate(GenerationRequest("test_repair", prompt, lambda: ""))
                    if result.used_llm and self._valid(result.text, fn):
                        new_text = result.text
                if new_text:
                    block.tests, block.used_llm = new_text, True
                    actions.append({"function": block.function, "outcome": "llm_repair", "tests": failing})
                else:
                    remaining = remove_functions(block.tests, set(failing))
                    if remaining.strip():
                        block.tests = remaining
                    else:
                        module.blocks.remove(block)
                    actions.append({"function": block.function, "outcome": "quarantined", "tests": failing})
            module.path.write_text(module.render(), encoding="utf-8")
        for action in actions:
            self.log("repair", **action)
        return actions
