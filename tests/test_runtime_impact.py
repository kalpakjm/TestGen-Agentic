import json
import unittest

from helpers import REPO, SAMPLES

from ptagent.agents.base import Trace
from ptagent.agents.runtime_impact import RuntimeImpactAgent, parse_version
from ptagent.config import load_config
from ptagent.tools.analyzer import build_index


class RuntimeImpactTests(unittest.TestCase):
    def setUp(self):
        self.agent = RuntimeImpactAgent(Trace(), load_config())
        self.index = build_index(SAMPLES / "legacy_runtime")

    def test_version_parsing(self):
        self.assertLess(parse_version("13.3"), parse_version("16.0"))
        self.assertEqual(parse_version("17.3 LTS"), (17, 3))

    def test_findings_match_hand_labels(self):
        labels = json.loads((REPO / "data/labels/legacy_runtime.json").read_text())
        report = self.agent.analyze(self.index, "13.3", "17.3")
        found = {(f["file"], f["rule_id"], f["location"]) for f in report["findings"]}
        self.assertEqual(found, {tuple(x) for x in labels["findings"]})

    def test_version_range_controls_which_rules_apply(self):
        report = self.agent.analyze(self.index, "13.3", "15.4")
        ids = {f["rule_id"] for f in report["findings"]}
        self.assertNotIn("R001", ids)  # python 3.12 rule is assumed to start at DBR 16.0
        self.assertNotIn("R002", ids)  # ANSI default is assumed to start at DBR 17.0
        self.assertIn("R003", ids)     # deprecation rules always apply

    def test_notebooks_are_reported(self):
        report = self.agent.analyze(self.index, "13.3", "17.3")
        self.assertIn("notebooks/legacy_daily_job.ipynb", report["impacted_notebooks"])
        self.assertIn("notebooks/legacy_report.py", report["impacted_notebooks"])

    def test_tests_importing_impacted_modules_are_listed(self):
        index = build_index(SAMPLES / "orders_with_tests")
        report = self.agent.analyze(index, "13.3", "17.3")
        self.assertIn("tests/test_cleaning.py", report["tests_to_review"])  # cleaning.py uses cast / to_date


if __name__ == "__main__":
    unittest.main()
