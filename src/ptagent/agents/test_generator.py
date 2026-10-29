"""Test generation agent (Mode A, and gap filling in Mode B).

For every eligible function it prepares context (inferred schema + retrieved guidance), asks the
LLM backend for pytest tests, validates the answer and falls back to a deterministic template when
the answer is unusable. With the default offline backend the template is always used.
"""
from __future__ import annotations

import ast
import builtins
import json
import os
import re
from pathlib import Path
from string import Template

from ..llm import GenerationRequest
from ..models import FunctionTests, GeneratedModule
from ..tools.analyzer import notebook_extract_source
from ..tools.columns import RANK, SAMPLE_VALUES, infer_columns
from .base import Agent

PRIMITIVE_SAMPLES = {"str": '"a"', "int": "1", "float": "1.0", "bool": "True"}
PATHISH = re.compile(r"(path|file|dir|uri|url)", re.I)
OUTPUT_NAME = re.compile(r"(^|_)(out|output|dest|destination|target|sink)(_|$)", re.I)
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
# Paths given by ptagent (for example a cleaned copy of the project) must win over the defaults above.
_extras = [p for p in os.environ.get("PTAGENT_PYTHONPATH", "").split(os.pathsep) if p]
for _extra in reversed(_extras):
    sys.path[:] = [p for p in sys.path if os.path.normcase(p) != os.path.normcase(_extra)]
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
        .config("spark.sql.ansi.enabled", os.environ.get("PTAGENT_ANSI", "false"))
        .getOrCreate()
    )
'''

SPARK_HELPERS = '''def _make(spark, schema, rows):
    """Build a DataFrame with an explicit schema (no inference). Whole numbers become floats in double columns."""
    names = [f.name for f in schema.fields]
    doubles = {f.name for f in schema.fields if isinstance(f.dataType, T.DoubleType)}

    def fix(name, value):
        return float(value) if name in doubles and isinstance(value, int) and not isinstance(value, bool) else value

    fixed = []
    for row in rows:
        if hasattr(row, "asDict"):
            row = row.asDict()
        if isinstance(row, dict):
            fixed.append({k: fix(k, v) for k, v in row.items()})
        else:
            fixed.append(tuple(fix(n, v) for n, v in zip(names, row)))
    return spark.createDataFrame(fixed, schema=schema)


def _materialize(result):
    """Force execution of lazy DataFrames so errors surface inside the test."""
    if isinstance(result, DataFrame):
        result.collect()
    return result
'''


FILE_HELPERS = '''def _write_csv(path, columns, rows):
    """Write a small CSV (header plus rows) and return its path in a form Spark can read."""
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)
    return Path(path).as_posix()
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


def extract_functions_source(source: str, names) -> str:
    """Source text of the named top-level functions (with decorators)."""
    tree = ast.parse(source)
    lines = source.splitlines()
    chunks = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            chunks.append("\n".join(lines[start - 1 : node.end_lineno]))
    return "\n\n\n".join(chunks) + ("\n" if chunks else "")


def salvage_complete_functions(text: str) -> str:
    """Keep only the complete top-level functions of an answer that may have been cut off mid-way."""
    candidate = text
    while candidate.strip():
        try:
            ast.parse(candidate)
            return candidate
        except SyntaxError:
            cut = candidate.rfind("\ndef ")
            if cut == -1:
                return ""
            candidate = candidate[:cut]
    return ""


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
        self._project_columns = {}
        self.generation_prompt = Template((cfg.prompts_dir / "test_generation.txt").read_text(encoding="utf-8"))
        self.pure_prompt = Template((cfg.prompts_dir / "test_generation_pure.txt").read_text(encoding="utf-8"))
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
        files = {"input": [], "output": []}
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
                if base not in {"str", ""}:
                    return None, f"parameter '{p.name}' looks like a path but is typed {annotation}"
                role = self._path_role(fn, p.name)
                value = f"{role}_{len(files[role]) + 1}"
                files[role].append(value)
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
            "files": files,
            "file_columns": dict(self._project_columns or {"value": "string"}) if files["input"] else {},
        }, None

    @staticmethod
    def _path_role(fn, name: str) -> str:
        """Decide whether a path parameter is read from (input) or written to (output)."""
        if OUTPUT_NAME.search(name) or "output" in name.lower():
            return "output"
        source = fn.source
        writes = ".write" in source or ".save(" in source
        reads = ".read" in source
        return "output" if writes and not reads else "input"

    def _union_columns(self, index) -> dict:
        """Columns used by any DataFrame function in the project, for synthetic input files."""
        merged = {}
        for fn in index.functions:
            takes_df = any((p.annotation or "").endswith("DataFrame") or p.name in self.cfg.dataframe_param_names
                           for p in fn.params)
            if not takes_df:
                continue
            numeric = [p.name for p in fn.params if (p.annotation or "").split("[")[0] in {"int", "float"}]
            columns = infer_columns(fn.source, numeric).columns
            if columns == {"value": "string"}:
                continue
            for name, kind in columns.items():
                if name.startswith("_"):
                    continue
                if RANK[kind] >= RANK.get(merged.get(name, "string"), 0):
                    merged[name] = kind
        return merged

    # ------------------------------------------------------------------ rendering
    @staticmethod
    def _constants(fn, plan) -> str:
        key = fn.name.upper()
        parts = []
        if plan["kind"] == "spark":
            cols = sorted(plan["columns"].items())
            fields = ", ".join(f'T.StructField("{name}", {SPARK_TYPES[kind]}, True)' for name, kind in cols)
            rows = [tuple(SAMPLE_VALUES[kind][i] for _, kind in cols) for i in range(2)]
            parts.append(
                f"_SCHEMA_{key} = T.StructType([{fields}])\n"
                f"_ROWS_{key} = {rows!r}\n"
                f"_NULL_ROWS_{key} = [(None,) * {len(cols)}, (None,) * {len(cols)}]\n"
            )
        if plan.get("files", {}).get("input"):
            cols = sorted(plan["file_columns"].items())
            names = [name for name, _ in cols]
            def as_text(kind, value):
                # whole-number text ("10", not "10.0") so casts to int work with ANSI mode on or off
                return str(int(value)) if kind == "numeric" else str(value)

            rows = [tuple(as_text(kind, SAMPLE_VALUES[kind][i]) for _, kind in cols) for i in range(2)]
            parts.append(f"_FILE_COLUMNS_{key} = {names!r}\n_FILE_ROWS_{key} = {rows!r}\n")
        return "\n".join(parts)

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
        files = plan.get("files", {"input": [], "output": []})
        if files["input"] or files["output"]:
            params = (["spark"] if plan["kind"] != "pure" else []) + ["tmp_path"]
            setup = [f"{v} = _make(spark, _SCHEMA_{key}, _ROWS_{key})" for v in plan["df_vars"]]
            setup += [f'{v} = _write_csv(tmp_path / "input_{i}.csv", _FILE_COLUMNS_{key}, _FILE_ROWS_{key})'
                      for i, v in enumerate(files["input"], 1)]
            setup += [f'{v} = (tmp_path / "output_{i}").as_posix()' for i, v in enumerate(files["output"], 1)]
            runner = f"_materialize({call})" if plan["kind"] != "pure" else call
            checks = [asserts] + [f"assert os.path.isdir({v}) and os.listdir({v})" for v in files["output"]]
            body = "\n    ".join(setup + [f"result = {runner}"] + checks)
            return f"def test_{name}_with_file_fixtures({', '.join(params)}):\n    {body}\n"
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
    def _normalize(self, text: str, fn, plan, existing_names=()):
        """Validate an LLM answer and keep only usable test functions.

        Imports, fixtures and helpers are dropped. The answer is rejected when it has no
        test_<function>* tests, uses fixtures or names that do not exist in the generated file,
        or does not parse. Returns (clean source or None, reason when rejected).
        """
        salvaged = salvage_complete_functions(text)
        if not salvaged.strip():
            return None, "answer is not valid Python (no complete function found)"
        text = salvaged
        tree = ast.parse(text)
        tests = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name.startswith(f"test_{fn.name}")]
        if not tests:
            return None, f"no test functions named test_{fn.name}*"
        allowed_params = {"spark"} if plan["kind"] != "pure" else set()
        known = set(dir(builtins)) | {fn.name, "pytest", "spark", "_make", "_materialize", "DataFrame", "Row", "T", "F"}
        known |= set(re.findall(r"^(_\w+) =", self._constants(fn, plan), re.M))
        lines = text.splitlines()
        chunks = []
        for node in tests:
            extra = {a.arg for a in node.args.args} - allowed_params
            if extra:
                return None, f"{node.name} uses fixtures that do not exist: {sorted(extra)}"
            loads = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            local = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))}
            local |= {a.arg for a in ast.walk(node) if isinstance(a, ast.arg)}
            unknown = loads - local - known
            if unknown:
                return None, f"{node.name} uses undefined names: {sorted(unknown)}"
            has_check = any(isinstance(n, ast.Assert) for n in ast.walk(node)) or "raises" in ast.dump(node)
            if not has_check:
                return None, f"{node.name} has no assertion"
            if node.name in existing_names:
                continue
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            chunks.append("\n".join(lines[start - 1 : node.end_lineno]))
        if not chunks:
            return None, "all tests duplicate the template tests"
        return "\n\n\n".join(chunks[:4]) + "\n", None

    def _generate_block(self, fn, plan):
        constants = self._constants(fn, plan)
        base_tests = self._template(fn, plan)
        if any(plan.get("files", {}).values()):
            return FunctionTests(fn.name, constants, base_tests, False,
                                 "file-fixture tests use the template only"), []
        context_chunks = self.kb.search(self._query(fn, plan), k=2) if self.kb else []
        context = "\n".join(f"- {c.title}: {c.text}" for c in context_chunks) or "(none)"
        signature = ", ".join(f"{p.name}: {p.annotation}" if p.annotation else p.name for p in fn.params)
        template = self.pure_prompt if plan["kind"] == "pure" else self.generation_prompt
        prompt = template.safe_substitute(
            module=fn.module,
            function_source=fn.source,
            function_name=fn.name,
            signature=f"{fn.name}({signature})" + (f" -> {fn.returns}" if fn.returns else ""),
            call=f"{fn.name}({plan['call_args']})",
            columns=json.dumps(plan["columns"]) if plan["columns"] else "(not a DataFrame function)",
            constants=", ".join(re.findall(r"^(_\w+) =", constants, re.M)) or "(none)",
            context=context,
            existing_tests=base_tests,
        )
        result = self.backend.generate(GenerationRequest("test_generation", prompt, lambda: ""))
        tests, used_llm, note, rejected = base_tests, False, result.note, ""
        if result.used_llm:
            extra, reason = self._normalize(result.text, fn, plan, set(test_names(base_tests)))
            if extra:
                tests = base_tests.rstrip() + "\n\n\n" + extra
                used_llm, note = True, f"{len(test_names(extra))} LLM tests added ({result.note})"
            else:
                note = f"LLM answer rejected: {reason}; template tests only"
                rejected = result.text
        elif not result.note.startswith("deterministic"):
            note = f"{result.note}; template tests only"
        return FunctionTests(fn.name, constants, tests, used_llm, note, rejected), [c.id for c in context_chunks]

    @staticmethod
    def _query(fn, plan) -> str:
        tokens = re.findall(r"[A-Za-z_]+", fn.source)
        return " ".join(tokens) + " null empty dataframe test"

    def generate(self, index, functions, out_dir: Path):
        """Generate test modules for the given functions. Returns (modules, skipped, retrieval)."""
        out_dir = Path(out_dir)
        tests_dir = out_dir / "generated_tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        self._project_columns = self._union_columns(index)
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
                header += "from pyspark.sql import DataFrame, Row, functions as F, types as T  # noqa: F401\n"
            header += f"\nfrom {module} import {names}\n"
            uses_input_files = any(plan["files"]["input"] for _, plan in items)
            uses_output_files = any(plan["files"]["output"] for _, plan in items)
            if uses_output_files:
                header = header.replace("import pytest  # noqa: F401\n", "import os\n\nimport pytest  # noqa: F401\n", 1)
            if uses_input_files:
                header = header.replace("import pytest  # noqa: F401\n", "import csv\nfrom pathlib import Path\n\nimport pytest  # noqa: F401\n", 1)
            if any(plan["kind"] == "spark" or plan["kind"] == "spark_only" for _, plan in items):
                header += "\n\n" + SPARK_HELPERS
            if uses_input_files:
                header += "\n\n" + FILE_HELPERS
            for block in blocks:
                if block.rejected_text:
                    rejected_dir = out_dir / "llm_rejections"
                    rejected_dir.mkdir(parents=True, exist_ok=True)
                    (rejected_dir / f"{module}.{block.function}.txt").write_text(block.rejected_text, encoding="utf-8")
            path = tests_dir / f"test_{module.replace('.', '_')}_generated.py"
            generated = GeneratedModule(module, path, header, blocks)
            path.write_text(generated.render(), encoding="utf-8")
            modules.append(generated)
            self.log("generated tests", module=module, functions=[fn.name for fn, _ in items],
                     backend=self.backend.name, llm_used=any(b.used_llm for b in blocks),
                     notes={b.function: b.note for b in blocks})

        roots = [Path(r) for r in index.source_roots] + path_entries
        relative = sorted({os.path.relpath(r, tests_dir) for r in roots})
        (tests_dir / "conftest.py").write_text(CONFTEST.replace("__PATHS__", repr(relative)), encoding="utf-8")
        for item in skipped:
            self.log("skipped function", **item)
        self._unload()
        return modules, skipped, retrieval

    # ------------------------------------------------------------------ repair
    def repair(self, modules, failures, index, quarantine_only=False):
        """Fix or quarantine failing generated tests. failures: {test name: message}. Returns actions."""
        actions = []
        for module in modules:
            for block in list(module.blocks):
                names = test_names(block.tests)
                failing = [n for n in names if n in failures]
                if not failing:
                    continue
                fn = next((f for f in index.functions if f.name == block.function and f.module == module.target_module), None)
                new_text, reject = None, None
                if fn is not None and self.backend.name == "ollama" and not quarantine_only:
                    plan, _ = self.plan(fn)
                    prompt = self.repair_prompt.safe_substitute(
                        function_name=block.function, tests=block.tests,
                        failures="\n".join(f"{n}: {failures[n]}" for n in failing),
                        function_source=fn.source)
                    result = self.backend.generate(GenerationRequest("test_repair", prompt, lambda: ""))
                    if result.used_llm and plan:
                        new_text, reject = self._normalize(result.text, fn, plan, set(names) - set(failing))
                removed_source = extract_functions_source(block.tests, set(failing))
                remaining = remove_functions(block.tests, set(failing))
                if new_text:
                    block.tests = remaining.rstrip() + "\n\n\n" + new_text
                    block.used_llm = True
                    actions.append({"function": block.function, "outcome": "llm_repair", "tests": failing})
                else:
                    if remaining.strip():
                        block.tests = remaining
                    else:
                        module.blocks.remove(block)
                    messages = {n: failures[n] for n in failing}
                    folder = module.path.parent.parent / "quarantined_tests"
                    folder.mkdir(parents=True, exist_ok=True)
                    header = "".join(f"# {name}: {message}\n" for name, message in messages.items())
                    (folder / f"{module.target_module}.{block.function}.py").write_text(
                        "# Tests removed because they failed. Failure messages:\n" + header + "\n" + removed_source,
                        encoding="utf-8")
                    actions.append({"function": block.function, "outcome": "quarantined", "tests": failing,
                                    "messages": {n: failures[n][:400] for n in failing},
                                    "messages": messages,
                                    "reason": "still failing after the repair attempts" if quarantine_only else (reject or "offline backend")})
            module.path.write_text(module.render(), encoding="utf-8")
        for action in actions:
            self.log("repair", **action)
        self._unload()
        return actions

    def _unload(self):
        """Free the model's memory before Spark starts (only backends that support it)."""
        unload = getattr(self.backend, "unload", None)
        if unload:
            unload()
