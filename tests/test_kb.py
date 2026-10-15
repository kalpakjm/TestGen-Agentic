import unittest

from ptagent.config import load_config
from ptagent.kb import KnowledgeBase


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        cfg = load_config()
        self.kb = KnowledgeBase.build(cfg.guidelines_path, cfg.dbr_rules_path)

    def test_index_has_guidelines_and_rules(self):
        sources = {c.source for c in self.kb.chunks}
        self.assertEqual(sources, {"testing_guidelines", "dbr_rules"})

    def test_window_query_retrieves_window_guidance(self):
        top = self.kb.search("row_number rank window partitionBy ordering", k=1)[0]
        self.assertIn("window", top.title.lower())

    def test_cast_query_retrieves_cast_guidance(self):
        titles = [c.title.lower() for c in self.kb.search("cast int to_date null invalid ANSI", k=3)]
        self.assertTrue(any("cast" in t or "ansi" in t for t in titles))


if __name__ == "__main__":
    unittest.main()
