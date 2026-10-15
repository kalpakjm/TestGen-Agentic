"""Coverage-gap agent (Mode B): decides which functions still need tests."""
from __future__ import annotations

from pathlib import Path

from .base import Agent


class CoverageGapAgent(Agent):
    name = "coverage_gap"

    def analyze(self, index, coverage_result=None, exclude=()) -> dict:
        """Static gaps (functions no test references) plus line-level gaps when coverage data exists."""
        exclude = set(exclude)
        untested = [
            fn.qualname for fn in index.functions
            if fn.name not in index.test_references and not fn.name.startswith("_") and fn.qualname not in exclude
        ]
        uncovered_by_function = {}
        if coverage_result and coverage_result.get("uncovered"):
            missing_by_path = {str(Path(p).resolve()): lines for p, lines in coverage_result["uncovered"].items()}
            for fn in index.functions:
                if fn.origin != "module" or fn.qualname in exclude:
                    continue
                missing = missing_by_path.get(str((index.root / fn.file).resolve()))
                if not missing:
                    continue
                lines = [n for n in missing if fn.lineno <= n <= fn.end_lineno]
                if lines:
                    uncovered_by_function[fn.qualname] = lines
        targets = list(dict.fromkeys(untested + list(uncovered_by_function)))
        self.log("coverage gap analysis", untested=len(untested), with_uncovered_lines=len(uncovered_by_function))
        return {"untested_functions": untested, "uncovered_by_function": uncovered_by_function, "targets": targets}
