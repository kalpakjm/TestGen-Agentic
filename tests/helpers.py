"""Shared helpers for the framework tests."""
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SAMPLES = REPO / "data" / "sample_projects"
