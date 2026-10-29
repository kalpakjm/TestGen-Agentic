import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from helpers import SAMPLES

from ptagent.agents.base import Trace
from ptagent.agents.obsolete_code import ObsoleteCodeAgent
from ptagent.agents.orchestrator import Orchestrator, coverage_verdict
from ptagent.config import load_config
from ptagent.tools.analyzer import build_index

PROJECT = {
    "src/pkg/__init__.py": "",
    "src/pkg/core.py": (
        "from pyspark.sql.functions import pandas_udf\n\n\n"
        "def used():\n    return 1\n\n\n"
        "@pandas_udf('double')\ndef registered_udf(s):\n    return s\n\n\n"
        "def kept_helper():  # ptagent: keep\n    return 2\n\n\n"
        "def only_tests_use_me():\n    return 3\n\n\n"
        "def truly_dead():\n    return 4\n\n\n"
        "def main():\n    return used()\n"
    ),
    "tests/test_core.py": "from pkg.core import only_tests_use_me\n\n\ndef test_x():\n    assert only_tests_use_me() == 3\n",
}


def write_project(root: Path):
    for rel, text in PROJECT.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "proj"
        write_project(self.root)
        self.cfg = load_config()
        self.index = build_index(self.root, self.cfg.skip_dirs)
        self.agent = ObsoleteCodeAgent(Trace(), self.cfg)
        self.report = self.agent.analyze(self.index)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_unreferenced_unprotected_functions_are_dead(self):
        dead = {d["name"]: d["confidence"] for d in self.report["dead_functions"]}
        self.assertEqual(dead, {"only_tests_use_me": "medium", "truly_dead": "high"})

    def test_decorated_and_marked_functions_are_protected(self):
        reasons = {p["function"].split(".")[-1]: p["reason"] for p in self.report["protected_functions"]}
        self.assertIn("pandas_udf", reasons["registered_udf"])
        self.assertIn("ptagent: keep", reasons["kept_helper"])

    def test_keep_functions_config_protects_by_name(self):
        self.cfg.keep_functions = ["truly_dead"]
        report = ObsoleteCodeAgent(Trace(), self.cfg).analyze(self.index)
        self.assertNotIn("truly_dead", [d["name"] for d in report["dead_functions"]])

    def test_only_high_confidence_functions_are_removed(self):
        out = Path(self.tmp.name) / "out"
        self.agent.apply(self.index, self.report, out)
        cleaned = (out / "refactored/src/pkg/core.py").read_text()
        self.assertNotIn("truly_dead", cleaned)
        self.assertIn("only_tests_use_me", cleaned)
        self.assertIn("registered_udf", cleaned)
        self.assertIn("kept_helper", cleaned)

    def test_patch_applies_cleanly_to_the_project(self):
        out = Path(self.tmp.name) / "out"
        self.agent.apply(self.index, self.report, out)
        patch = out / "cleanup.patch"
        self.assertIn("--- a/src/pkg/core.py", patch.read_text())
        if shutil.which("patch"):
            result = subprocess.run(["patch", "-p1", "--dry-run", "-i", str(patch)], cwd=self.root,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_in_place_apply_needs_confirmation_and_keeps_backups(self):
        out = Path(self.tmp.name) / "out"
        self.agent.apply(self.index, self.report, out)
        source = self.root / "src/pkg/core.py"
        original = source.read_text()
        self.assertEqual(self.agent.apply_in_place(self.index, out, lambda n: False), [])
        self.assertEqual(source.read_text(), original)
        changed = self.agent.apply_in_place(self.index, out, lambda n: True)
        self.assertEqual(changed, ["src/pkg/core.py"])
        self.assertNotIn("truly_dead", source.read_text())
        self.assertEqual((self.root / "src/pkg/core.py.bak").read_text(), original)


class ConftestPathTests(unittest.TestCase):
    def test_paths_from_ptagent_take_priority_over_defaults_even_if_already_on_sys_path(self):
        import os
        import sys
        import types
        from ptagent.agents.test_generator import CONFTEST

        fake_pytest = types.ModuleType("pytest")
        fake_pytest.fixture = lambda *a, **k: (lambda f: f)
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "original_src"
            cleaned = Path(tmp) / "cleaned_src"
            original.mkdir()
            cleaned.mkdir()
            source = CONFTEST.replace("__PATHS__", repr([os.path.relpath(original, tmp)]))
            saved_path, saved_env = list(sys.path), os.environ.get("PTAGENT_PYTHONPATH")
            saved_module = sys.modules.get("pytest")
            try:
                sys.modules["pytest"] = fake_pytest
                sys.path.append(str(cleaned))  # already present, as with PYTHONPATH
                os.environ["PTAGENT_PYTHONPATH"] = str(cleaned)
                exec(compile(source, "conftest.py", "exec"), {"__file__": str(Path(tmp) / "conftest.py")})
                self.assertEqual(sys.path[0], str(cleaned))
                self.assertLess(sys.path.index(str(cleaned)), sys.path.index(str(original.resolve())))
            finally:
                sys.path[:] = saved_path
                if saved_env is None:
                    os.environ.pop("PTAGENT_PYTHONPATH", None)
                else:
                    os.environ["PTAGENT_PYTHONPATH"] = saved_env
                if saved_module is None:
                    sys.modules.pop("pytest", None)
                else:
                    sys.modules["pytest"] = saved_module


class CoverageTests(unittest.TestCase):
    def test_verdicts(self):
        self.assertEqual(coverage_verdict(None, 80), "not measured")
        self.assertEqual(coverage_verdict(80.0, 80), "PASS")
        self.assertEqual(coverage_verdict(79.7, 80), "FAIL (0.3 points short)")

    def test_cleaned_project_copy_lacks_dead_code_and_original_is_untouched(self):
        cfg = load_config()
        index = build_index(SAMPLES / "orders_with_tests", cfg.skip_dirs)
        agent = ObsoleteCodeAgent(Trace(), cfg)
        report = agent.analyze(index)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            agent.apply(index, report, out)
            cleaned_root = Orchestrator(cfg)._build_cleaned_project(index, out)
            self.assertNotIn("legacy_normalize_status", (cleaned_root / "src/orders_etl/cleaning.py").read_text())
            self.assertTrue((cleaned_root / "tests/conftest.py").exists())
        self.assertIn("legacy_normalize_status", (SAMPLES / "orders_with_tests/src/orders_etl/cleaning.py").read_text())

    def test_offline_mode_b_reports_not_measured_without_spark(self):
        cfg = load_config()
        cfg.run_tests = "never"
        with tempfile.TemporaryDirectory() as tmp:
            report = Orchestrator(cfg).run(SAMPLES / "orders_with_tests", mode="B", out_root=Path(tmp))
            self.assertEqual(report["coverage"]["verdict"], "not measured")
            self.assertEqual(len(report["obsolete_code"]["dead_functions"]), 3)
            self.assertTrue((Path(tmp) / "orders_with_tests" / "cleanup.patch").exists())


if __name__ == "__main__":
    unittest.main()
