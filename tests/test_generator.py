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

    def test_plan_skips_entry_points(self):
        plan, reason = self.agent.plan(self.fn("main"))
        self.assertIsNone(plan)
        self.assertIn("entry point", reason)

    def test_path_parameters_get_file_fixtures(self):
        plan, _ = self.agent.plan(self.fn("read_orders"))
        self.assertEqual(plan["files"], {"input": ["input_1"], "output": []})
        plan, _ = self.agent.plan(self.fn("write_parquet"))
        self.assertEqual(plan["files"], {"input": [], "output": ["output_1"]})
        plan, _ = self.agent.plan(self.fn("run_pipeline"))
        self.assertEqual(plan["files"], {"input": ["input_1"], "output": ["output_1"]})
        self.assertEqual(plan["call_args"], "spark, input_1, output_1")

    def test_file_fixture_tests_are_generated_and_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            modules, skipped, _ = self.agent.generate(self.index, self.index.functions, Path(tmp))
            self.assertEqual([s["function"] for s in skipped], ["orders_etl.pipeline.main"])
            by_module = {m.target_module: m.path.read_text() for m in modules}
            pipeline = by_module["orders_etl.pipeline"]
            self.assertIn("def test_run_pipeline_with_file_fixtures(spark, tmp_path)", pipeline)
            self.assertIn("_write_csv(tmp_path", pipeline)
            self.assertIn("os.listdir(output_1)", pipeline)
            self.assertIn("order_id", pipeline)  # columns taken from the other functions in the project
            storage = by_module["orders_etl.storage"]
            self.assertIn("test_write_parquet_with_file_fixtures", storage)
            self.assertIn("assert result is None", storage)
            for source in by_module.values():
                compile(source, "generated", "exec")

    def test_generated_conftest_pins_ansi_mode(self):
        from ptagent.agents.test_generator import CONFTEST
        self.assertIn("spark.sql.ansi.enabled", CONFTEST)

    def test_plan_ignores_defaulted_parameters(self):
        plan, _ = self.agent.plan(self.fn("top_customers"))
        self.assertEqual(plan["call_args"], "df")
        self.assertEqual(plan["kind"], "spark")

    def test_generated_files_are_valid_python(self):
        with tempfile.TemporaryDirectory() as tmp:
            modules, skipped, _ = self.agent.generate(self.index, self.index.functions, Path(tmp))
            self.assertEqual(len(skipped), 1)
            self.assertEqual(len(modules), 6)
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

    def test_normalize_rejects_the_real_model_answer_that_used_dataframe_fixtures(self):
        answer = """import pytest
from orders_etl.utils import safe_divide

@pytest.fixture
def df_with_values(spark):
    return spark.createDataFrame([(10.0, 2.0)], ["numerator", "denominator"])

def test_safe_divide_normal_input(df_with_values):
    result = safe_divide(df_with_values)
    assert result.count() == 2
"""
        fn = self.fn("safe_divide")
        plan, _ = self.agent.plan(fn)
        clean, reason = self.agent._normalize(answer, fn, plan)
        self.assertIsNone(clean)
        self.assertIn("fixtures that do not exist", reason)

    def test_normalize_keeps_good_tests_and_drops_imports_and_duplicates(self):
        answer = """import pytest
from orders_etl.utils import safe_divide

def test_safe_divide_returns_expected_type():
    assert safe_divide(1.0, 1.0) == 1.0

def test_safe_divide_zero_denominator_returns_zero():
    assert safe_divide(5.0, 0.0) == 0.0

def test_safe_divide_halves():
    assert safe_divide(1.0, 2.0) == pytest.approx(0.5)
"""
        fn = self.fn("safe_divide")
        plan, _ = self.agent.plan(fn)
        existing = set(extract_test_names(self.agent._template(fn, plan)))
        clean, reason = self.agent._normalize(answer, fn, plan, existing)
        self.assertIsNone(reason)
        self.assertNotIn("import", clean)
        self.assertNotIn("test_safe_divide_returns_expected_type", clean)
        self.assertIn("zero_denominator", clean)
        compile(clean, "x", "exec")

    def test_normalize_rejects_undefined_names(self):
        answer = "def test_safe_divide_x():\n    assert helper(1) == 2\n"
        fn = self.fn("safe_divide")
        plan, _ = self.agent.plan(fn)
        clean, reason = self.agent._normalize(answer, fn, plan)
        self.assertIsNone(clean)
        self.assertIn("undefined names", reason)

    def test_llm_tests_are_added_to_template_tests(self):
        from ptagent.llm import GenerationResult

        class FakeBackend:
            name = "ollama"

            def generate(self, request):
                return GenerationResult("def test_safe_divide_zero_denominator():\n    assert safe_divide(5.0, 0.0) == 0.0\n",
                                        True, "fake model")

        agent = make_agent()
        agent.backend = FakeBackend()
        fn = self.fn("safe_divide")
        plan, _ = agent.plan(fn)
        block, _ = agent._generate_block(fn, plan)
        names = extract_test_names(block.tests)
        self.assertTrue(block.used_llm)
        self.assertEqual(len(names), 3)
        self.assertIn("test_safe_divide_is_deterministic", names)

    def test_rejected_answer_keeps_template_and_records_reason(self):
        from ptagent.llm import GenerationResult

        class BadBackend:
            name = "ollama"

            def generate(self, request):
                return GenerationResult("def helper():\n    pass\n", True, "fake model")

        agent = make_agent()
        agent.backend = BadBackend()
        fn = self.fn("safe_divide")
        plan, _ = agent.plan(fn)
        block, _ = agent._generate_block(fn, plan)
        self.assertFalse(block.used_llm)
        self.assertIn("rejected", block.note)
        self.assertEqual(len(extract_test_names(block.tests)), 2)

    def test_truncated_answer_keeps_the_complete_tests(self):
        from ptagent.agents.test_generator import salvage_complete_functions
        cut = "def test_a():\n    assert 1 == 1\n\ndef test_b():\n    assert 2 == 2\n\ndef test_c():\n    x = {'k': '2023-01"
        kept = salvage_complete_functions(cut)
        self.assertEqual(extract_test_names(kept), ["test_a", "test_b"])
        self.assertEqual(salvage_complete_functions("def broken(:\n"), "")

    def test_unclosed_code_fence_is_handled(self):
        self.assertEqual(extract_code("```python\nx = 1\ny = 2"), "x = 1\ny = 2")
        self.assertEqual(extract_code("```\nx = 1\n```"), "x = 1")

    def test_rejected_answer_text_is_saved_for_inspection(self):
        from ptagent.llm import GenerationResult

        class BadBackend:
            name = "ollama"

            def generate(self, request):
                return GenerationResult("def helper():\n    pass\n", True, "fake model")

        agent = make_agent()
        agent.backend = BadBackend()
        with tempfile.TemporaryDirectory() as tmp:
            agent.generate(self.index, [self.fn("safe_divide")], Path(tmp))
            saved = list((Path(tmp) / "llm_rejections").glob("*.txt"))
            self.assertEqual(len(saved), 1)
            self.assertIn("def helper", saved[0].read_text())

    def test_whole_numbers_become_floats_in_double_columns(self):
        from ptagent.agents.test_generator import SPARK_HELPERS

        class Double: pass
        class Text: pass
        class Field:
            def __init__(self, name, data_type): self.name, self.dataType = name, data_type
        class Schema:
            def __init__(self, fields): self.fields = fields
        class Types: DoubleType = Double
        class Spark:
            def createDataFrame(self, rows, schema=None): return rows

        namespace = {"T": Types, "DataFrame": object}
        exec(SPARK_HELPERS, namespace)
        schema = Schema([Field("a", Text()), Field("n", Double())])
        self.assertEqual(namespace["_make"](Spark(), schema, [("x", 2)]), [("x", 2.0)])
        self.assertEqual(namespace["_make"](Spark(), schema, [{"a": "x", "n": 4}]), [{"a": "x", "n": 4.0}])

    def test_quarantine_only_removes_failing_tests_without_calling_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            modules, _, _ = self.agent.generate(self.index, [self.fn("safe_divide")], Path(tmp))
            actions = self.agent.repair(modules, {"test_safe_divide_is_deterministic": "boom"}, self.index,
                                        quarantine_only=True)
            self.assertEqual(actions[0]["outcome"], "quarantined")
            self.assertIn("still failing", actions[0]["reason"])

    def test_model_is_unloaded_after_generation_and_repair(self):
        from ptagent.llm import GenerationResult

        class Backend:
            name = "ollama"
            unloads = 0

            def generate(self, request):
                return GenerationResult("", False, "stub")

            def unload(self):
                Backend.unloads += 1

        agent = make_agent()
        agent.backend = Backend()
        with tempfile.TemporaryDirectory() as tmp:
            modules, _, _ = agent.generate(self.index, [self.fn("safe_divide")], Path(tmp))
            agent.repair(modules, {"test_safe_divide_is_deterministic": "x"}, self.index, quarantine_only=True)
        self.assertEqual(Backend.unloads, 2)

    def test_row_is_available_to_generated_tests(self):
        with tempfile.TemporaryDirectory() as tmp:
            modules, _, _ = self.agent.generate(self.index, [self.fn("add_order_total")], Path(tmp))
            self.assertIn("import DataFrame, Row, functions as F", modules[0].path.read_text())
        from ptagent.llm import GenerationResult

        class Backend:
            name = "ollama"

            def generate(self, request):
                return GenerationResult("def test_add_order_total_row(spark):\n    assert Row(a=1).a == 1\n", True, "stub")

        agent = make_agent()
        agent.backend = Backend()
        fn = self.fn("add_order_total")
        plan, _ = agent.plan(fn)
        block, _ = agent._generate_block(fn, plan)
        self.assertTrue(block.used_llm)

    def test_quarantined_tests_are_saved_with_their_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            modules, _, _ = self.agent.generate(self.index, [self.fn("safe_divide")], Path(tmp))
            actions = self.agent.repair(modules, {"test_safe_divide_is_deterministic": "AssertionError: x"},
                                        self.index, quarantine_only=True)
            self.assertEqual(actions[0]["messages"], {"test_safe_divide_is_deterministic": "AssertionError: x"})
            saved = (Path(tmp) / "quarantined_tests" / "orders_etl.utils.safe_divide.py").read_text()
            self.assertIn("AssertionError: x", saved)
            self.assertIn("def test_safe_divide_is_deterministic", saved)
            compile(saved, "x", "exec")

    def test_row_objects_are_accepted_and_converted_to_dicts(self):
        from ptagent.agents.test_generator import SPARK_HELPERS
        from ptagent.llm import GenerationResult

        class Double: pass
        class Text: pass
        class Field:
            def __init__(self, name, data_type): self.name, self.dataType = name, data_type
        class Schema:
            def __init__(self, fields): self.fields = fields
        class Types: DoubleType = Double
        class FakeRow:
            def asDict(self): return {"n": 3, "a": "x"}
        class Spark:
            def createDataFrame(self, rows, schema=None): return rows

        namespace = {"T": Types, "DataFrame": object}
        exec(SPARK_HELPERS, namespace)
        schema = Schema([Field("a", Text()), Field("n", Double())])
        self.assertEqual(namespace["_make"](Spark(), schema, [FakeRow()]), [{"n": 3.0, "a": "x"}])

        answer = "def test_status_counts_x(spark):\n    df = _make(spark, _SCHEMA_STATUS_COUNTS, [Row(status='a')])\n    assert df is not None\n"
        fn = self.fn("status_counts")
        plan, _ = self.agent.plan(fn)
        clean, reason = self.agent._normalize(answer, fn, plan)
        self.assertIsNone(reason)

    def test_quarantine_records_failure_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            modules, _, _ = self.agent.generate(self.index, [self.fn("safe_divide")], Path(tmp))
            actions = self.agent.repair(modules, {"test_safe_divide_is_deterministic": "assert 1 == 2"}, self.index,
                                        quarantine_only=True)
            self.assertEqual(actions[0]["messages"], {"test_safe_divide_is_deterministic": "assert 1 == 2"})

    def test_tests_without_assertions_are_rejected(self):
        answer = "def test_safe_divide_noop():\n    safe_divide(1.0, 2.0)\n"
        fn = self.fn("safe_divide")
        plan, _ = self.agent.plan(fn)
        clean, reason = self.agent._normalize(answer, fn, plan)
        self.assertIsNone(clean)
        self.assertIn("no assertion", reason)

    def test_llm_helpers(self):
        self.assertEqual(extract_code("text\n```python\nx = 1\n```\n"), "x = 1")
        backend = OllamaBackend("http://127.0.0.1:9", "none", timeout_s=1)
        from ptagent.llm import GenerationRequest
        result = backend.generate(GenerationRequest("t", "p", lambda: "fallback"))
        self.assertEqual(result.text, "fallback")
        self.assertFalse(result.used_llm)


if __name__ == "__main__":
    unittest.main()
