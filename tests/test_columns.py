import unittest

from ptagent.tools.columns import infer_columns


class ColumnInferenceTests(unittest.TestCase):
    def test_numeric_from_arithmetic_and_aggregates(self):
        src = "def f(df):\n    return df.withColumn('t', F.col('q') * F.col('p')).groupBy('k').agg(F.sum('amt'))"
        cols = infer_columns(src).columns
        self.assertEqual(cols["q"], "numeric")
        self.assertEqual(cols["p"], "numeric")
        self.assertEqual(cols["amt"], "numeric")
        self.assertEqual(cols["k"], "string")

    def test_cast_and_date_functions(self):
        src = "def f(df):\n    return df.withColumn('a', F.col('a').cast('int')).withColumn('d', F.to_date(F.col('d')))"
        cols = infer_columns(src).columns
        self.assertEqual(cols["a"], "numeric_string")
        self.assertEqual(cols["d"], "date")

    def test_numeric_parameter_comparison(self):
        src = "def f(df, n):\n    return df.filter(F.col('rank') <= n)"
        self.assertEqual(infer_columns(src, numeric_params=["n"]).columns["rank"], "numeric")

    def test_fallback_when_nothing_is_found(self):
        self.assertEqual(infer_columns("def f(df):\n    return df").columns, {"value": "string"})


if __name__ == "__main__":
    unittest.main()
