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
        self.assertEqual([f["kind"] for f in result["failures"]], ["failure", "error"])
        self.assertEqual(result["failures"][0]["detail"], "trace")
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


class VerifierCommandTests(unittest.TestCase):
    def run_and_capture(self, ignore):
        from unittest import mock
        cfg = load_config()
        cfg.ignore_project_addopts = ignore
        agent = VerifierAgent(Trace(), cfg)
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.setdefault("cmd", cmd)
            raise OSError("stop here")

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(VerifierAgent, "dynamic_status", return_value=(True, "")), \
                mock.patch("subprocess.run", fake_run):
            try:
                agent.run_tests([Path(tmp) / "t.py"], [Path(tmp)], [], Path(tmp), "x")
            except OSError:
                pass
        return captured["cmd"]

    def test_the_projects_own_pytest_options_are_ignored_by_default(self):
        cmd = self.run_and_capture(True)
        self.assertEqual(cmd[cmd.index("-o") + 1], "addopts=")

    def test_the_projects_own_pytest_options_can_be_kept(self):
        self.assertNotIn("-o", self.run_and_capture(False))

    def test_spark_workers_use_the_same_python_as_the_driver(self):
        import os
        import sys
        from unittest import mock
        cfg = load_config()
        agent = VerifierAgent(Trace(), cfg)
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.setdefault("env", kwargs["env"])
            raise OSError("stop here")

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"PYSPARK_PYTHON": "C:/Python39/python.exe"}), \
                mock.patch.object(VerifierAgent, "dynamic_status", return_value=(True, "")), \
                mock.patch("subprocess.run", fake_run):
            try:
                agent.run_tests([Path(tmp) / "t.py"], [Path(tmp)], [], Path(tmp), "x")
            except OSError:
                pass
        self.assertEqual(captured["env"]["PYSPARK_PYTHON"], sys.executable)
        self.assertTrue(any(e["action"] == "PYSPARK_PYTHON replaced" for e in agent.trace.events))

    def test_tests_run_from_the_project_folder(self):
        from unittest import mock
        agent = VerifierAgent(Trace(), load_config())
        captured = {}

        def fake_run(cmd, **kwargs):
            captured.setdefault("cwd", kwargs["cwd"])
            raise OSError("stop here")

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(VerifierAgent, "dynamic_status", return_value=(True, "")), \
                mock.patch("subprocess.run", fake_run):
            try:
                agent.run_tests([Path(tmp) / "t.py"], [Path(tmp)], [], Path(tmp) / "work", "x", cwd=Path(tmp) / "proj")
            except OSError:
                pass
        self.assertEqual(captured["cwd"], Path(tmp) / "proj")


if __name__ == "__main__":
    unittest.main()
