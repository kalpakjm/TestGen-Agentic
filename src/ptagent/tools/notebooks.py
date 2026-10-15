"""Helpers for reading Databricks notebooks (.ipynb and the '# Databricks notebook source' .py export)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

DATABRICKS_MARKER = "# Databricks notebook source"
NON_PYTHON_MAGICS = ("%sql", "%md", "%sh", "%scala", "%r", "%fs", "%pip", "%conda")


def is_databricks_source(path: Path) -> bool:
    """True for the .py export format used by Databricks notebooks."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.readline().strip() == DATABRICKS_MARKER
    except (OSError, UnicodeDecodeError):
        return False


def sanitize_cell(source: str) -> Optional[str]:
    """Make a notebook cell parseable as Python.

    Whole-cell non-Python magics (%sql, %md, ...) return None. Inline magics (%run, !cmd)
    are replaced by 'pass' so that line numbers stay stable.
    """
    lines = source.splitlines()
    first = next((line.strip() for line in lines if line.strip()), "")
    if first.startswith(NON_PYTHON_MAGICS):
        return None
    cleaned = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("%") or stripped.startswith("!"):
            indent = line[: len(line) - len(stripped)]
            cleaned.append(f"{indent}pass  # magic removed")
        else:
            cleaned.append(line)
    return "\n".join(cleaned)


def load_ipynb_cells(path: Path) -> list:
    """Return code cells as dicts: {index, source}. 'index' counts all cells (markdown included)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cells = []
    for index, cell in enumerate(data.get("cells", [])):
        if cell.get("cell_type") != "code":
            continue
        source = cell.get("source", "")
        if isinstance(source, list):
            source = "".join(source)
        cells.append({"index": index, "source": source})
    return cells
