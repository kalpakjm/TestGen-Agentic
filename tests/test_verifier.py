import json
import tempfile
import unittest
from pathlib import Path

from ptagent.agents.base import Trace
from ptagent.agents.verifier import VerifierAgent
from ptagent.config import load_config

JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest" tests="4" failures="1" errors="1" skipped="1">
<testcase classname="gen.test_a_generated" name="test_ok"/>
<testcase classname="gen.test_a_generated" name="test_bad"><failure message="AssertionError: boom">trace</failure></testcase>
<testcase classname="gen.test_b_generated" name="test_err"><error message="ImportError: nope">trace</error></testcase>
<testcase classname="gen.test_c" name="test_skip"><skipped message="s"/></testcase>
</testsuite></testsuites>"""


class VerifierParsingTests(unittest.TestCase):
    def test_junit_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "junit.xml"
            path.write_text(JUNIT)
            result = VerifierAgent._parse_junit(path)
        self.assertEqual((result["total"], result["passed"], result["failed"], result["errors"], result["skipped"]),
                         (4, 1, 1, 1, 1))
        self.assertEqual(result["failures"][0]["name"], "test_bad")
        self.assertTrue(result["failures"][0]["file"].endswith("_generated"))

    def test_coverage_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coverage.json"
            path.write_text(json.dumps({"totals": {"percent_covered": 81.234},
                                        "files": {"/x/a.py": {"missing_lines": [3, 4]}}}))
            result = VerifierAgent._parse_coverage(path)
        self.assertEqual(result["coverage_percent"], 81.2)
        self.assertEqual(result["uncovered"], {"/x/a.py": [3, 4]})

    def test_dynamic_run_can_be_disabled(self):
        cfg = load_config()
        cfg.run_tests = "never"
        agent = VerifierAgent(Trace(), cfg)
        result = agent.run_tests([], [], [], Path(tempfile.gettempdir()), "x")
        self.assertEqual(result["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
