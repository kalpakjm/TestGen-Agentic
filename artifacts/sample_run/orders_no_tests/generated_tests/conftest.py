"""Shared fixtures for ptagent generated tests."""
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
for _relative in ['../../../../data/sample_projects/orders_no_tests/src', '../extracted']:
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
