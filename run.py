#!/usr/bin/env python3
"""Entry point: python run.py demo | run <project> | analyze <project> | evaluate"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ptagent.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
