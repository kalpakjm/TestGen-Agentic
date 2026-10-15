# Planned enhancements for the final submission

1. **Assertion-rich tests.** The LLM proposes expected values from the function body and docstring; the verifier runs them against the real function and keeps only tests that pass and fail when the function is mutated.
2. **Richer fixtures.** Temporary-file fixtures for IO functions, support for several DataFrame parameters with different schemas, and `spark.sql.ansi.enabled` pinned per test.
3. **Mode B depth.** Branch-level gap targeting from coverage data (the baseline reads line-level gaps), and optional application of the cleaned copies as a patch.
4. **Mode C test migration.** For each impacted notebook or module, rewrite or add the tests that cover the changed behaviour (for example null versus error on bad casts under ANSI mode), re-run, and report. Code migration is an optional add-on, shown as a diff.
5. **Rule base.** Grow `dbr_rules.json` from official release notes and Spark migration guides, with source links and verified version windows.
6. **Evaluation.** Larger corpus, mutation testing, backend comparison (see `docs/evaluation.md`).
7. **Packaging.** Installable package (`pyproject.toml`) and a Databricks Asset Bundle example for running the agents inside a workspace.
