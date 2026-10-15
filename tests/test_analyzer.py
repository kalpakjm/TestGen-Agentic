import unittest

from helpers import SAMPLES

from ptagent.tools.analyzer import build_index, collect_references, extract_functions, parse_source


class AnalyzerTests(unittest.TestCase):
    def test_extracts_functions_with_signatures(self):
        source = "def f(df: DataFrame, n: int = 3, *, flag: bool = False) -> DataFrame:\n    '''doc'''\n    return df\n"
        fn = extract_functions(parse_source(source), source, "m", "m.py")[0]
        self.assertEqual([p.name for p in fn.params], ["df", "n", "flag"])
        self.assertEqual(fn.params[1].default, "3")
        self.assertEqual(fn.params[2].kind, "kwonly")
        self.assertEqual(fn.returns, "DataFrame")
        self.assertEqual(fn.docstring, "doc")

    def test_references_include_calls_attributes_and_imports(self):
        tree = parse_source("from a import b\nx = c.d(e())\n")
        self.assertTrue({"b", "d", "c", "e"} <= collect_references(tree))

    def test_index_of_sample_projects(self):
        index = build_index(SAMPLES / "orders_no_tests")
        self.assertEqual(len(index.functions), 15)
        self.assertFalse(index.has_tests)
        self.assertEqual(len(index.notebooks), 1)
        notebook_functions = [f for f in index.functions if f.origin == "notebook"]
        self.assertEqual([f.name for f in notebook_functions], ["status_counts"])
        self.assertEqual(notebook_functions[0].module, "nb_orders_exploration")

    def test_existing_tests_are_detected(self):
        index = build_index(SAMPLES / "orders_with_tests")
        self.assertTrue(index.has_tests)
        self.assertEqual(len(index.test_functions), 4)

    def test_magic_cells_do_not_break_parsing(self):
        index = build_index(SAMPLES / "orders_no_tests")
        self.assertEqual(index.warnings, [])


if __name__ == "__main__":
    unittest.main()
