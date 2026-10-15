import tempfile
import unittest
from pathlib import Path

from helpers import SAMPLES

from ptagent.agents.base import Trace
from ptagent.agents.coverage_gap import CoverageGapAgent
from ptagent.agents.obsolete_code import ObsoleteCodeAgent
from ptagent.config import load_config
from ptagent.tools.analyzer import build_index


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.index = build_index(SAMPLES / "orders_with_tests")

    def test_dead_functions(self):
        report = ObsoleteCodeAgent(Trace(), self.cfg).analyze(self.index)
        dead = {d["function"] for d in report["dead_functions"]}
        self.assertEqual(dead, {"orders_etl.cleaning.legacy_normalize_status",
                                "orders_etl.utils.normalize_country", "orders_etl.utils.is_weekend"})

    def test_stale_test_import(self):
        report = ObsoleteCodeAgent(Trace(), self.cfg).analyze(self.index)
        self.assertEqual([(s["test_file"], s["missing_name"]) for s in report["stale_test_imports"]],
                         [("tests/test_legacy_tax.py", "legacy_compute_tax")])
        self.assertEqual(report["stale_test_imports"][0]["tests"], ["test_legacy_compute_tax_adds_eight_percent"])

    def test_apply_writes_copies_without_touching_sources(self):
        agent = ObsoleteCodeAgent(Trace(), self.cfg)
        report = agent.analyze(self.index)
        original = (self.index.root / "src/orders_etl/cleaning.py").read_text()
        with tempfile.TemporaryDirectory() as tmp:
            agent.apply(self.index, report, Path(tmp))
            cleaned = (Path(tmp) / "refactored/src/orders_etl/cleaning.py").read_text()
            self.assertNotIn("legacy_normalize_status", cleaned)
            self.assertIn("clean_orders", cleaned)
            stale = (Path(tmp) / "refactored/tests/test_legacy_tax.py").read_text()
            self.assertNotIn("legacy_compute_tax", stale)
        self.assertEqual(original, (self.index.root / "src/orders_etl/cleaning.py").read_text())

    def test_untested_functions_exclude_dead_code(self):
        dead = {"orders_etl.cleaning.legacy_normalize_status", "orders_etl.utils.normalize_country",
                "orders_etl.utils.is_weekend", "orders_etl.pipeline.main"}
        gaps = CoverageGapAgent(Trace()).analyze(self.index, exclude=dead)
        self.assertIn("orders_etl.aggregations.daily_revenue", gaps["untested_functions"])
        self.assertNotIn("orders_etl.cleaning.clean_orders", gaps["untested_functions"])
        self.assertEqual(len(gaps["untested_functions"]), 8)

    def test_line_level_gaps_from_coverage_data(self):
        fn = next(f for f in self.index.functions if f.name == "daily_revenue")
        coverage = {"uncovered": {str(self.index.root / fn.file): [fn.lineno + 2]}}
        gaps = CoverageGapAgent(Trace()).analyze(self.index, coverage)
        self.assertIn("orders_etl.aggregations.daily_revenue", gaps["uncovered_by_function"])


if __name__ == "__main__":
    unittest.main()
