import ast
import tempfile
import unittest
from pathlib import Path

from helpers import SAMPLES

from ptagent.agents.base import Trace
from ptagent.agents.test_generator import TestGeneratorAgent, remove_functions
from ptagent.agents.test_generator import test_names as extract_test_names
from ptagent.config import load_config
from ptagent.kb import KnowledgeBase
from ptagent.llm import OfflineBackend, OllamaBackend, extract_code
from ptagent.tools.analyzer import build_index


def make_agent():
    cfg = load_config()
    kb = KnowledgeBase.build(cfg.guidelines_path, cfg.dbr_rules_path)
    return TestGeneratorAgent(Trace(), cfg, OfflineBackend(), kb)


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.agent = make_agent()
        self.index = build_index(SAMPLES / "orders_no_tests")

    def fn(self, name):
        return next(f for f in self.index.functions if f.name == name)

    def test_plan_skips_path_parameters_and_entry_points(self):
        for name in ("read_orders", "write_parquet", "run_pipeline", "main"):
            plan, reason = self.agent.plan(self.fn(name))
            self.assertIsNone(plan, name)
            self.assertTrue(reason)

    def test_plan_ignores_defaulted_parameters(self):
        plan, _ = self.agent.plan(self.fn("top_customers"))
        self.assertEqual(plan["call_args"], "df")
        self.assertEqual(plan["kind"], "spark")

    def test_generated_files_are_valid_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            modules, skipped, _ = self.agent.generate(self.index, self.index.functions, Path(tmp))
            self.assertEqual(len(skipped), 4)
            self.assertEqual(len(modules), 4)
            for module in modules:
                tree = ast.parse(module.path.read_text())
                self.assertTrue(extract_test_names(module.path.read_text()))
                self.assertIsNotNone(tree)
            self.assertTrue((Path(tmp) / "generated_tests" / "conftest.py").exists())
            self.assertTrue((Path(tmp) / "extracted" / "nb_orders_exploration.py").exists())

    def test_pure_function_tests_use_annotations(self):
        plan, _ = self.agent.plan(self.fn("safe_divide"))
        text = self.agent._template(self.fn("safe_divide"), plan)
        self.assertIn("isinstance(result, float)", text)
        self.assertIn("safe_divide(1.0, 1.0)", text)

    def test_remove_functions_keeps_the_rest(self):
        src = "def a():\n    pass\n\n\ndef b():\n    pass\n"
        self.assertNotIn("def a", remove_functions(src, {"a"}))
        self.assertIn("def b", remove_functions(src, {"a"}))

    def test_repair_quarantines_failing_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            modules, _, _ = self.agent.generate(self.index, [self.fn("safe_divide")], Path(tmp))
            actions = self.agent.repair(modules, {"test_safe_divide_is_deterministic": "boom"}, self.index)
            self.assertEqual(actions[0]["outcome"], "quarantined")
            self.assertNotIn("is_deterministic", modules[0].path.read_text())
            self.assertIn("returns_expected_type", modules[0].path.read_text())

    def test_llm_helpers(self):
        self.assertEqual(extract_code("text\n```python\nx = 1\n```\n"), "x = 1")
        backend = OllamaBackend("http://127.0.0.1:9", "none", timeout_s=1)
        from ptagent.llm import GenerationRequest
        result = backend.generate(GenerationRequest("t", "p", lambda: "fallback"))
        self.assertEqual(result.text, "fallback")
        self.assertFalse(result.used_llm)


if __name__ == "__main__":
    unittest.main()
