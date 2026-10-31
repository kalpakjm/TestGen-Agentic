"""Small helpers for Databricks Runtime and Spark version numbers."""
from __future__ import annotations

import re


def parse_version(text) -> tuple:
    """'17.3 LTS' -> (17, 3). Missing parts become 0."""
    numbers = [int(n) for n in re.findall(r"\d+", str(text))][:2]
    return tuple(numbers + [0] * (2 - len(numbers)))


def fmt(version: tuple) -> str:
    return f"{version[0]}.{version[1]}"
