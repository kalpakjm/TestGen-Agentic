"""Runtime-impact agent (Mode C): finds notebooks and modules affected by a Databricks Runtime upgrade."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from ..models import Finding
from ..tools.analyzer import module_name_for
from ..tools.notebooks import load_ipynb_cells, sanitize_cell
from .base import Agent


def parse_version(text: str) -> tuple:
    numbers = [int(n) for n in re.findall(r"\d+", str(text))][:2]
    return tuple(numbers + [0] * (2 - len(numbers)))


def dotted(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    return "(expr)"


def matches(node, matcher: dict) -> bool:
    kind = matcher["type"]
    if kind == "import":
        names = set(matcher["names"])
        if isinstance(node, ast.Import):
            return any(a.name.split(".")[0] in names for a in node.names)
        if isinstance(node, ast.ImportFrom):
            return bool(node.module) and node.level == 0 and node.module.split(".")[0] in names
        return False
    if kind == "import_from_name":
        return isinstance(node, ast.ImportFrom) and any(a.name in matcher["names"] for a in node.names)
    if kind == "name":
        return isinstance(node, ast.Name) and node.id in matcher["names"]
    if kind == "dotted_suffix":
        if isinstance(node, ast.Attribute):
            text = dotted(node)
            return any(text == n.lstrip(".") or text.endswith("." + n.lstrip(".")) for n in matcher["names"])
        if isinstance(node, ast.Name):
            return node.id in [n for n in matcher["names"] if "." not in n]
        return False
    if kind == "string_contains":
        return (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and any(s in node.value for s in matcher["substrings"]))
    if kind == "cast_to":
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "cast" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                return arg.value.lower().split("(")[0] in matcher["types"]
        return False
    return False


class RuntimeImpactAgent(Agent):
    name = "runtime_impact"

    def __init__(self, trace, cfg, kb=None):
        super().__init__(trace)
        self.cfg = cfg
        self.kb = kb
        self.rules = json.loads(Path(cfg.dbr_rules_path).read_text(encoding="utf-8"))["rules"]

    def _applicable_rules(self, from_dbr: str, to_dbr: str) -> list:
        lower, upper = parse_version(from_dbr), parse_version(to_dbr)
        chosen = []
        for rule in self.rules:
            applies = rule["applies"]
            if applies["type"] == "always":
                chosen.append((rule, "always (deprecated or restricted API)"))
            elif lower < parse_version(applies["introduced_in"]) <= upper:
                chosen.append((rule, f"introduced in DBR {applies['introduced_in']}"))
        return chosen

    @staticmethod
    def _source_lines(index, rel: str) -> dict:
        path = index.root / rel
        if rel.endswith(".ipynb"):
            lines = {}
            for cell in load_ipynb_cells(path):
                text = sanitize_cell(cell["source"])
                if text is not None:
                    lines[cell["index"]] = text.splitlines()
            return lines
        return {None: path.read_text(encoding="utf-8").splitlines()}

    def analyze(self, index, from_dbr: str, to_dbr: str) -> dict:
        rules = self._applicable_rules(from_dbr, to_dbr)
        test_rels = {index.rel(p) for p in index.test_files}
        findings, seen = [], set()
        for rel, trees in index.trees.items():
            if rel in test_rels:
                continue
            lines = self._source_lines(index, rel)
            for cell, tree in trees:
                for node in ast.walk(tree):
                    if not hasattr(node, "lineno"):
                        continue
                    for rule, why in rules:
                        if any(matches(node, m) for m in rule["matchers"]):
                            location = f"cell{cell}:L{node.lineno}" if cell is not None else f"L{node.lineno}"
                            key = (rel, rule["id"], location)
                            if key in seen:
                                continue
                            seen.add(key)
                            text = (lines.get(cell) or [""])[node.lineno - 1] if node.lineno - 1 < len(lines.get(cell) or []) else ""
                            findings.append(Finding(rule["id"], rule["title"], rel, location, text.strip()[:120],
                                                    rule["severity"], rule["recommendation"], why))
        findings.sort(key=lambda f: (f.file, f.location, f.rule_id))
        impacted_files = sorted({f.file for f in findings})
        impacted_modules = {module_name_for(index.root / rel, index.source_roots) for rel in impacted_files}
        impacted_tests = sorted({
            test_file for test_file, module, _ in index.test_imports
            if any(module == m or module.startswith(m + ".") for m in impacted_modules)
        })
        actions = self._actions(rules, findings)
        self.log("runtime impact analysis", from_dbr=from_dbr, to_dbr=to_dbr, findings=len(findings),
                 impacted_files=len(impacted_files), impacted_tests=len(impacted_tests))
        return {
            "from_dbr": from_dbr, "to_dbr": to_dbr,
            "rules_checked": [{"id": r["id"], "title": r["title"], "reason": why} for r, why in rules],
            "findings": [f.__dict__ for f in findings],
            "impacted_files": impacted_files,
            "impacted_notebooks": [f for f in impacted_files if f.endswith(".ipynb") or f in {index.rel(n) for n in index.notebooks}],
            "tests_to_review": impacted_tests,
            "actions": actions,
            "note": "Rules are a starter set; validate them against the official release notes. "
                    "Test migration and optional code migration are planned for the final submission.",
        }

    def _actions(self, rules, findings) -> list:
        actions = []
        for rule, _ in rules:
            files = sorted({f.file for f in findings if f.rule_id == rule["id"]})
            if not files:
                continue
            guidance = []
            if self.kb:
                guidance = [c.text for c in self.kb.search(rule["title"] + " " + rule["description"] + " test", k=3)
                            if c.source == "testing_guidelines"][:1]
            actions.append({"rule_id": rule["id"], "title": rule["title"], "files": files,
                            "recommendation": rule["recommendation"], "test_guidance": guidance})
        return actions
