# Planned enhancements for the final submission

1. **Assertion-rich tests.** The LLM proposes expected values from the function body and docstring; the verifier runs them against the real function and keeps only tests that pass and fail when the function is mutated.
2. **Richer fixtures.** Done: temporary-file fixtures for functions with string path parameters, and `spark.sql.ansi.enabled` pinned in the generated session (override with `PTAGENT_ANSI=true`). Still planned: several DataFrame parameters with different schemas, other file formats, and value-checking assertions for file-based functions.
3. **Mode B depth.** Branch-level gap targeting from coverage data (the baseline reads line-level gaps), and optional application of the cleaned copies as a patch.
4. **Mode C test migration.** Baseline done for the ANSI-mode change (existing tests run under an ANSI emulation, migrated tests written, model rewrites proposed but not executed). Still planned: executing and scoring the model's proposed rewrites, emulation for more behaviour changes, and notebook-level tests. Code migration is an optional add-on, shown as a diff.
5. **Rule base.** Done: rules are derived from the official release notes for any upgrade (heuristic extraction). Still planned: run it against the live pages and tune the extraction, let the model refine extracted rules (validated against a schema), and add plugins for more options than ANSI mode.
6. **Evaluation.** Larger corpus, mutation testing, backend comparison (see `docs/evaluation.md`).
7. **Packaging.** Installable package (`pyproject.toml`) and a Databricks Asset Bundle example for running the agents inside a workspace.
