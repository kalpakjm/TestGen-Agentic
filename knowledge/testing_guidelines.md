# Guidelines for testing PySpark code

## Test setup and SparkSession fixture
Create one SparkSession per test session with a session-scoped pytest fixture. Use master local[1], disable the Spark UI and set spark.sql.shuffle.partitions to 1 so tests start fast and stay deterministic. Never connect to a real cluster from unit tests.

## Small deterministic DataFrames
Build input DataFrames from a few rows with an explicit StructType schema. Avoid schema inference because it hides type bugs and fails on empty or all-null columns. Keep each test focused on one behaviour.

## Null and empty input
Every transformation should be exercised with an empty DataFrame and with rows containing nulls. Assert that the function returns a DataFrame and does not raise. Aggregations over empty input can return zero rows or one row, so assert the type before asserting the count.

## Testing aggregations and groupBy
Assert on collected rows sorted by the grouping key. Compare numeric results with pytest.approx. A groupBy followed by agg changes the schema, so also assert on the output column names.

## Testing window functions
Window functions such as row_number, rank and dense_rank depend on ordering. Provide rows with distinct ordering values so the expected rank is unambiguous, and assert the ranking for each partition.

## Testing joins
Cover the matched, unmatched and duplicate-key cases. Check the row count after the join to catch accidental fan-out.

## Type casting and date parsing
Casting strings to int, double or date returns null for invalid input unless ANSI mode is enabled. Test both valid and invalid values, and pin spark.sql.ansi.enabled explicitly in the test session so behaviour does not change with the runtime.

## Deduplication
When keeping the latest record per key, supply two versions of the same key with different update timestamps and assert that only the newest version survives.

## Pure Python helpers
Helpers that do not touch Spark are tested with plain pytest functions. Check the return type, boundary values and determinism.

## Notebook code
Notebook cells cannot be imported. Move reusable functions into a module and test that module; keep notebooks as thin orchestration layers.
