"""Obsolete-code agent (Mode B): finds dead functions and stale tests, and writes a cleanup patch.

Safety rules: only functions that nothing references (confidence "high") are removed. Functions that only
tests reference ("medium"), functions with registering decorators, functions listed in keep_functions and
functions marked with "# ptagent: keep" are never removed. Source files are never modified unless the
caller explicitly asks for an in-place apply.
"""
from __future__ import annotations

import ast
import difflib
import shutil
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
    def _protected(self, fn):
        """Reason why a function must not be reported as dead, or None."""
        if fn.keep:
            return "marked with '# ptagent: keep'"
        if fn.name in self.cfg.keep_functions or fn.qualname in self.cfg.keep_functions:
            return "listed in keep_functions"
        for decorator in fn.decorators:
            base = decorator.split("(")[0].strip()
            if any(base == k or base.endswith("." + k) for k in self.cfg.keep_decorators):
                return f"decorated with @{base}"
        return None

    def analyze(self, index) -> dict:
        entry = set(self.cfg.entry_point_names)
        dead, protected = [], []
        for fn in index.functions:
            if fn.name.startswith("__") or fn.name in entry or fn.name.startswith("test"):
                continue
            if fn.name in index.code_references:
                continue
            reason = self._protected(fn)
            if reason:
                protected.append({"function": fn.qualname, "file": fn.file, "reason": reason})
                continue
            used_by_tests = fn.name in index.test_references
            dead.append({
                "function": fn.qualname, "name": fn.name, "file": fn.file,
                "lines": f"{fn.lineno}-{fn.end_lineno}", "origin": fn.origin,
                "referenced_by_tests": used_by_tests,
                "confidence": "medium" if used_by_tests else "high",
            })
        stale = self._stale_test_imports(index)
        self.log("obsolete code analysis", dead_functions=len(dead), protected=len(protected),
                 stale_test_imports=len(stale))
        return {"dead_functions": dead, "protected_functions": protected, "stale_test_imports": stale}

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
        """Write cleaned copies under out_dir/refactored and a unified diff in out_dir/cleanup.patch.

        Sources are never modified. Returns the list of changed files.
        """
        out_dir = Path(out_dir)
        refactored = out_dir / "refactored"
        if refactored.exists():
            shutil.rmtree(refactored)
        patch_file = out_dir / "cleanup.patch"
        if patch_file.exists():
            patch_file.unlink()
        written, diffs = [], []
        dead_by_file = {}
        for item in report["dead_functions"]:
            if item["origin"] == "module" and item["confidence"] == "high":
                dead_by_file.setdefault(item["file"], set()).add(item["name"])
        stale_by_file = {}
        for item in report["stale_test_imports"]:
            entry = stale_by_file.setdefault(item["test_file"], {"names": set(), "tests": set()})
            entry["names"].add(item["missing_name"])
            entry["tests"].update(item["tests"])

        for rel, names in dead_by_file.items():
            original = (index.root / rel).read_text(encoding="utf-8")
            cleaned = remove_functions(original, names)
            written.append({"file": rel, "removed": sorted(names)})
            self._record(refactored / rel, original, cleaned, rel, diffs)
        for rel, entry in stale_by_file.items():
            original = (index.root / rel).read_text(encoding="utf-8")
            cleaned = self._drop_imports(remove_functions(original, entry["tests"]), entry["names"])
            written.append({"file": rel, "removed": sorted(entry["tests"]) + [f"import {n}" for n in sorted(entry["names"])]})
            self._record(refactored / rel, original, cleaned, rel, diffs)
        if diffs:
            patch_file.write_text("".join(diffs), encoding="utf-8")
        for item in written:
            self.log("wrote cleaned copy", **item)
        return written

    @staticmethod
    def _record(path: Path, original: str, cleaned: str, rel: str, diffs: list):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cleaned, encoding="utf-8")
        diffs.extend(difflib.unified_diff(
            original.splitlines(keepends=True), cleaned.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}"))

    def apply_in_place(self, index, out_dir: Path, confirm) -> list:
        """Copy the cleaned files over the sources, keeping a .bak copy of each. Needs explicit confirmation."""
        refactored = Path(out_dir) / "refactored"
        files = [f for f in refactored.rglob("*") if f.is_file()] if refactored.exists() else []
        if not files:
            return []
        if not confirm(len(files)):
            self.log("in-place apply declined", files=len(files))
            return []
        changed = []
        for source in files:
            rel = source.relative_to(refactored)
            target = index.root / rel
            backup = target.with_name(target.name + ".bak")
            if not backup.exists():
                shutil.copy2(target, backup)
            shutil.copy2(source, target)
            changed.append(rel.as_posix())
        self.log("applied cleanup in place", files=changed)
        return changed

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
