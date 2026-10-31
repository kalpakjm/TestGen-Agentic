import json
import tempfile
import unittest
from pathlib import Path

from helpers import REPO, SAMPLES

from ptagent.agents.base import Trace
from ptagent.agents.orchestrator import Orchestrator
from ptagent.agents.runtime_impact import RuntimeImpactAgent, matches
from ptagent.config import load_config
from ptagent.tools.analyzer import build_index
import ast


def hits(rule_id, source):
    """Rule ids of the verified rules that match anything in a code snippet."""
    rules = json.loads((REPO / "knowledge/dbr_rules.json").read_text())["rules"]
    rule = next(r for r in rules if r["id"] == rule_id)
    tree = ast.parse(source)
    return any(matches(node, m) for node in ast.walk(tree) for m in rule["matchers"])


class RuleTests(unittest.TestCase):
    def test_every_verified_rule_names_a_source_and_evidence(self):
        rules = json.loads((REPO / "knowledge/dbr_rules.json").read_text())["rules"]
        for rule in rules:
            if rule["verified"]:
                self.assertTrue(rule["source"].startswith("https://"), rule["id"])
                self.assertTrue(rule["evidence"], rule["id"])

    def test_new_rules_match_their_patterns(self):
        self.assertTrue(hits("R009", "df.withColumn('f', F.input_file_name())"))
        self.assertTrue(hits("R009", "from pyspark.sql.functions import input_file_name\nx = input_file_name()"))
        self.assertTrue(hits("R010", "spark.readStream.format('cloudFiles').load(p)"))
        self.assertTrue(hits("R011", "for k, v in psdf.iteritems():\n    pass"))
        self.assertTrue(hits("R011", "psdf = df.to_pandas_on_spark()"))
        self.assertTrue(hits("R012", "import pyspark.pandas as ps"))
        self.assertTrue(hits("R012", "from pyspark import pandas as ps"))
        self.assertTrue(hits("R013", "spark.conf.set('spark.jars.packages', 'org.x:lib_2.12:1.0')"))

    def test_new_rules_do_not_match_unrelated_code(self):
        self.assertFalse(hits("R012", "import pyspark.sql.functions as F"))
        self.assertFalse(hits("R013", "x = 'lib_2.13'"))
        self.assertFalse(hits("R009", "name = 'input_file'"))

    def test_upgrade_path_selects_the_right_rules(self):
        agent = RuntimeImpactAgent(Trace(), load_config())
        ids = {r["id"] for r, _ in agent._applicable_rules("15.4", "17.3")}
        self.assertTrue({"R001", "R002", "R009", "R010", "R011", "R012", "R013"} <= ids)
        ids = {r["id"] for r, _ in agent._applicable_rules("15.4", "16.4")}
        self.assertIn("R001", ids)
        self.assertNotIn("R002", ids)
        self.assertNotIn("R009", ids)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.cfg.run_tests = "never"
        self.cfg.rules_mode = "curated"  # these tests use the curated rule file; no network
        self.tmp = tempfile.TemporaryDirectory()
        self.report = Orchestrator(self.cfg).run(SAMPLES / "orders_ansi_demo", mode="C", from_dbr="15.4",
                                                 to_dbr="17.3", out_root=Path(self.tmp.name))
        self.migration = self.report["migration"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_findings_match_hand_labels(self):
        labels = json.loads((REPO / "data/labels/orders_ansi_demo.json").read_text())
        found = {(f["file"], f["rule_id"], f["location"]) for f in self.report["runtime_impact"]["findings"]}
        self.assertEqual(found, {tuple(x) for x in labels["findings"]})

    def test_migrated_tests_are_written_for_the_ansi_sensitive_function(self):
        self.assertEqual(self.migration["migrated_functions"], ["orders_etl.cleaning.clean_orders"])
        self.assertEqual(sorted(self.migration["migrated_tests"]),
                         ["test_clean_orders_bad_input_becomes_null_when_ansi_is_off",
                          "test_clean_orders_bad_input_raises_when_ansi_is_on"])
        path = Path(self.tmp.name) / "orders_ansi_demo/migrated_tests/test_orders_etl_cleaning_ansi_migration.py"
        source = path.read_text()
        compile(source, "migrated", "exec")
        self.assertIn("pytest.raises(Exception)", source)
        self.assertIn("not-a-number", source)
        self.assertTrue((path.parent / "conftest.py").exists())

    def test_tests_that_cannot_run_are_reported_not_hidden(self):
        target = self.migration["existing_tests"]["target_runtime"]
        self.assertEqual(target["status"], "skipped")
        self.assertEqual(self.migration["broken_by_upgrade"], [])

    def test_legacy_project_without_testable_ansi_functions_gets_review_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = Orchestrator(self.cfg).run(SAMPLES / "legacy_runtime", mode="C", from_dbr="15.4",
                                                to_dbr="17.3", out_root=Path(tmp))
            rules = {item["rule"] for item in report["migration"]["review_items"]}
            self.assertIn("R001", rules)
            self.assertIn("R006", rules)
            ansi = [i for i in report["migration"]["review_items"] if i["rule"] == "R002"]
            self.assertEqual([i["files"] for i in ansi], [["notebooks/legacy_daily_job.ipynb"]])

    def test_model_proposals_are_saved_but_marked_unverified(self):
        from ptagent.llm import GenerationResult

        class Backend:
            name = "ollama"
            uses_llm = True

            def generate(self, request):
                return GenerationResult("def test_x(spark):\n    assert True\n", True, "fake")

        orchestrator = Orchestrator(self.cfg)
        orchestrator.migration.backend = Backend()
        index = build_index(SAMPLES / "orders_ansi_demo", self.cfg.skip_dirs)
        broken = [{"name": "test_clean_orders_turns_an_unparseable_quantity_into_null", "message": "boom"}]
        impact = self.report["runtime_impact"]
        with tempfile.TemporaryDirectory() as tmp:
            proposals = orchestrator.migration._propose(index, broken, {"R002": {"title": "ANSI"}}, impact, Path(tmp))
            self.assertEqual(len(proposals), 1)
            self.assertIn("UNVERIFIED", Path(proposals[0]["file"]).read_text())


if __name__ == "__main__":
    unittest.main()
