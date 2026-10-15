"""Obsolete-code agent (Mode B): finds dead functions and stale tests, and can write a cleaned copy."""
from __future__ import annotations

import ast
from pathlib import Path

from ..tools.analyzer import module_name_for
from .base import Agent
from .test_generator import remove_functions


class ObsoleteCodeAgent(Agent):
    name = "obsolete_code"

    def __init__(self, trace, cfg):
        super().__init__(trace)
        self.cfg = cfg

    # ------------------------------------------------------------------ analysis
    def analyze(self, index) -> dict:
        entry = set(self.cfg.entry_point_names)
        dead = []
        for fn in index.functions:
            if fn.name.startswith("__") or fn.name in entry or fn.name.startswith("test"):
                continue
            if fn.name in index.code_references:
                continue
            dead.append({
                "function": fn.qualname, "name": fn.name, "file": fn.file,
                "lines": f"{fn.lineno}-{fn.end_lineno}", "origin": fn.origin,
                "referenced_by_tests": fn.name in index.test_references,
            })
        stale = self._stale_test_imports(index)
        self.log("obsolete code analysis", dead_functions=len(dead), stale_test_imports=len(stale))
        return {"dead_functions": dead, "stale_test_imports": stale}

    def _project_module_names(self, index) -> dict:
        """module name -> set of top-level names defined or re-exported in it."""
        modules = {}
        for path in index.source_files:
            rel = index.rel(path)
            module = module_name_for(path, index.source_roots)
            names = set()
            for _, tree in index.trees.get(rel, []):
                for node in tree.body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        names.add(node.name)
                    elif isinstance(node, ast.Assign):
                        names.update(t.id for t in node.targets if isinstance(t, ast.Name))
                    elif isinstance(node, (ast.Import, ast.ImportFrom)):
                        names.update((a.asname or a.name).split(".")[0] for a in node.names)
            modules[module] = names
        return modules

    def _stale_test_imports(self, index) -> list:
        modules = self._project_module_names(index)
        stale = []
        for test_file, module, name in index.test_imports:
            if module not in modules or not name or name == "*":
                continue
            if name in modules[module] or f"{module}.{name}" in modules:
                continue
            affected = []
            for _, tree in index.trees.get(test_file, []):
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
                        if any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(node)):
                            affected.append(node.name)
            stale.append({"test_file": test_file, "module": module, "missing_name": name, "tests": affected})
        return stale

    # ------------------------------------------------------------------ apply
    def apply(self, index, report: dict, out_dir: Path) -> list:
        """Write cleaned copies of the affected files under out_dir/refactored. Sources are never modified."""
        out_dir = Path(out_dir) / "refactored"
        written = []
        dead_by_file = {}
        for item in report["dead_functions"]:
            if item["origin"] == "module":
                dead_by_file.setdefault(item["file"], set()).add(item["name"])
        for rel, names in dead_by_file.items():
            source = (index.root / rel).read_text(encoding="utf-8")
            self._write(out_dir / rel, remove_functions(source, names))
            written.append({"file": rel, "removed": sorted(names)})
        stale_by_file = {}
        for item in report["stale_test_imports"]:
            entry = stale_by_file.setdefault(item["test_file"], {"names": set(), "tests": set()})
            entry["names"].add(item["missing_name"])
            entry["tests"].update(item["tests"])
        for rel, entry in stale_by_file.items():
            source = (index.root / rel).read_text(encoding="utf-8")
            cleaned = remove_functions(source, entry["tests"])
            cleaned = self._drop_imports(cleaned, entry["names"])
            self._write(out_dir / rel, cleaned)
            written.append({"file": rel, "removed": sorted(entry["tests"]) + [f"import {n}" for n in sorted(entry["names"])]})
        for item in written:
            self.log("wrote cleaned copy", **item)
        return written

    @staticmethod
    def _write(path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    @staticmethod
    def _drop_imports(source: str, names) -> str:
        tree = ast.parse(source)
        lines = source.splitlines()
        edits = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and any(a.name in names for a in node.names):
                keep = [a for a in node.names if a.name not in names]
                replacement = []
                if keep:
                    node.names = keep
                    replacement = [ast.unparse(node)]
                edits.append((node.lineno, node.end_lineno, replacement))
        for start, end, replacement in sorted(edits, reverse=True):
            lines[start - 1 : end] = replacement
        return "\n".join(lines).strip("\n") + "\n"
