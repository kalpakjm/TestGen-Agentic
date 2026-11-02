# Evaluation

## What is measured
| Mode | Metric | Ground truth |
| --- | --- | --- |
| A | Precision/recall of which functions receive tests; share of generated files that parse; first-run pass rate and coverage (when Spark is available) | `data/labels/orders_no_tests.json` |
| B | Precision/recall for dead functions, stale test imports and untested functions; coverage before and after (when Spark is available) | `data/labels/orders_with_tests.json` |
| C | Precision/recall of findings (file, rule, location) and of impacted files | `data/labels/legacy_runtime.json` |

Run it with `python run.py evaluate`. Results are written to `outputs/evaluation_report.md` and `.json`; a reference run is in `artifacts/sample_run/`.

## Baseline result (offline backend, static parts only)
All precision and recall values on the three labelled samples are 1.0. Read this carefully: the samples are small, were written by the same person who wrote the rules, and the labels were written by hand from the sample code. The result shows that the baseline is wired correctly. It is not evidence of accuracy on other codebases.

Pass rate and coverage are reported as `skipped` in the reference run because the build environment had no Spark. On a machine with Spark they are filled in automatically.

## Mode B coverage on the sample project (real Spark)
On `orders_with_tests`, running only the existing tests gave 15.6% coverage and adding the 14 generated tests gave 43.8% in the first real run. After the file-fixture patch, the full Mode B run reached 79.7%. Phase 3 adds a third measurement on a copy of the project with the dead functions removed, and a PASS or FAIL verdict against `coverage_target`. Record the numbers of the final run in the report.

## Evaluation on real open-source repositories (Phase 5)

### Environment
All runs: Windows 11, 8 GB RAM, no GPU; Python 3.11 virtual environment; PySpark 3.5.9; Java 17; local Spark (`local[1]`).
LLM runs used Ollama with `qwen2.5-coder:3b`. DBR upgrade path for Mode C: 15.4 to 17.3, rules read from the official release notes.

Python 3.11 was chosen because DBR 15.4 runs Python 3.11 (17.3 runs 3.12). An earlier Python 3.9 environment could not import
two of the repositories at all (`X | None` type hints, compiled dependencies), which shows why the local Python should match the runtime.

Packages pinned for the repositories (not needed by the tool itself): `delta-spark==3.2.0`, `pandas<3`, `numpy<2`, `pyarrow<18`
(PyArrow 26 needs NumPy 2, which the repositories do not support), `setuptools<81` (keeps `pkg_resources`), `pandas`, `sqlglot`,
`databricks-sdk`, `databricks-labs-dqx`, `pyspark-test`.

### Repositories
| Repository | Licence | Used for | Notes |
| --- | --- | --- | --- |
| [ahsan-habib-sunny/databricks-medallion-pipeline](https://github.com/ahsan-habib-sunny/databricks-medallion-pipeline) | MIT | Mode C | 4 Databricks notebooks, no functions, no tests |
| [andre-salvati/databricks-template](https://github.com/andre-salvati/databricks-template) | Apache-2.0 | Mode B | 24 source files, 92 functions, 16 existing tests |
| [msukmanowsky/pyspark-testing](https://github.com/msukmanowsky/pyspark-testing) | MIT | Mode B | Python 2 code |
| [guillesd/example_pyspark_app](https://github.com/guillesd/example_pyspark_app) | none found | Mode B | analysed locally only; its code is not redistributed |

Record the commit of each clone for reproducibility (`git -C <repo> rev-parse HEAD`) and the clone date:

| Repository | Commit | Cloned on |
| --- | --- | --- |
| databricks-medallion-pipeline | (fill in) | (fill in) |
| databricks-template | (fill in) | (fill in) |
| pyspark-testing | (fill in) | (fill in) |
| example_pyspark_app | (fill in) | (fill in) |

### Results: offline backend
| Repository | Mode | Passed / failed / errors | Coverage (existing tests -> with generated tests) | Notes |
| --- | --- | --- | --- | --- |
| orders_no_tests (sample) | A | 33 / 0 / 0 | -> 84.4% | Same result in the Python 3.11 environment as before |
| example_pyspark_app | B | 4 / 1 / 0 | 84.4% -> 84.4% (target 80%: PASS) | The failure is the repository's own `test_main` (Delta overwrite); it fails without generated tests too |
| databricks-template | B | 24 / 0 / 1 | 11.1% -> 19.9% | The error needs Spark 4.1 (`from pyspark import pipelines`); not available in PySpark 3.5 |
| pyspark-testing | B | 0 / 0 / 4 | 23.0% -> 30.6% | Python 2 code (`unicode`); tests cannot run on Python 3, static results only |
| databricks-medallion-pipeline | C | n/a | n/a | 20 findings in 1 notebook, 3 review items |

Static Mode B results: 0 dead functions and 0 stale test imports in all three repositories; untested functions:
example_pyspark_app 2, databricks-template 45, pyspark-testing 5. Dead-code detection therefore has no real-repository evidence yet;
its evidence is still the labelled sample project.

### Results: offline templates versus the local LLM
| Repository | Offline | Ollama (`qwen2.5-coder:3b`) |
| --- | --- | --- |
| orders_no_tests (Mode A) | 33 tests, 33 passed, 84.4% | 59 tests, 59 passed, 87.5% |
| databricks-template (Mode B) | 20 generated, 24 passed, 19.9% | 37 generated, 29 passed, 20.4% |
| example_pyspark_app (Mode B) | 1 generated, 84.4% | 1 generated, 84.4% |
| databricks-medallion-pipeline (Mode C) | 20 findings | 20 findings |

The LLM produced more passing tests but little extra coverage: the additional tests run the same lines with concrete expected values.
Coverage therefore understates the difference; bug detection shows it (next section). Mode C detection is rule-based and does not
depend on the backend.

### Injected-bug check (does a generated test catch a real bug?)
`safe_divide` in `orders_no_tests` was changed from `numerator / denominator` to `numerator * denominator`, and the tests generated
from the correct code were run against the changed copy (`PTAGENT_PYTHONPATH` points the generated conftest at the copy).

| Tests | Result |
| --- | --- |
| Ollama-generated (15 tests in the module) | Caught: `test_safe_divide_positive_denominator` failed (`safe_divide(1.0, 2.0)` returned 2.0, expected 0.5) |
| Offline templates | Not run; the template only checks the return type and that two identical calls agree (`safe_divide(1.0, 1.0)`), which cannot detect this change |

### Mode C labels: databricks-medallion-pipeline, `notebooks/silver/silver_transformation.py`
Every finding was labelled by reading the flagged line. "Problem" means the line can raise an error once ANSI mode is on by default
(Spark 4.0, DBR 17.x) where it returns NULL today. Labels are in `data/labels/eval_medallion_silver.json`.

| Lines | Code | Label |
| --- | --- | --- |
| 88, 91 | `to_date(..., "yyyy-MM-dd")` with a fallback `to_date(..., "dd/MM/yyyy")` | True: the first parse raises before the fallback runs |
| 27, 158, 159, 234-237 | `.cast("int")` on raw CSV columns | Likely true: blank or decimal strings raise under ANSI |
| 177, 178, 361 | `to_date(column)` without a format | Unsure: depends on the data |
| 360 | `BDATE < to_date(lit("1900-01-01"))` | Unsure: the comparison casts the string column to a date |
| 242, 246, 250 | `to_date(..., "yyyyMMdd")` after a length-8 guard | Unsure: only 8-digit invalid dates still reach the parse |
| 282, 289, 302, 309 | `col("sls_sales") / col("sls_price")` and similar | Missed in the first pass: division by zero raises under ANSI |

| | First pass (16 findings) | After adding the division rule (20 findings) |
| --- | --- | --- |
| True or likely true | 9 | 13 |
| Unsure | 7 | 7 |
| False alarm | 0 | 0 |
| Known misses | 4 | 0 |
| Precision | 56% to 100% | 65% to 100% |
| Recall | 69% to 80% | 100% of the patterns labelled here |

The ranges depend on how the unsure lines resolve. The labels were made by the tool's author, so the after-fix recall only covers misses
known to the author; an independent labeller may find more. The repository also has a bug the tool did not need to find: lines 245
and 249 cast the length, not the column (`length(col(x)).cast("string") != 8`), which works only because `"8" != 8` is cast back.

### Mode C model proposal (orders_ansi_demo, Ollama)
The run found 4 impact findings, 1 existing test broken by the upgrade and wrote 2 migrated tests for `clean_orders` (raises with ANSI
on; NULL with ANSI off). For the broken test the model proposed a rewrite that only changed the docstring: the body still asserts that
the unparseable quantity becomes NULL, which fails under ANSI exactly like the original, and the file lacks its imports. The proposal
was saved as UNVERIFIED and not used, as designed. The template-based migrated tests are the reliable part of Mode C;
the 3B model added nothing on this task.

### Tool defects found by the real repositories and CI (all fixed)
The sample projects did not show any of these.

| # | Defect | Found on | Fix |
| --- | --- | --- | --- |
| 1 | The project's own pytest options (`--cov ...`) broke the test run | databricks-template | Patch 17: ignore project `addopts` (configurable) |
| 2 | Code outside `src/` (`scripts/project_costs.py`) could not be imported | databricks-template | Patch 18: its folder becomes an import root |
| 3 | `setup.py` was treated as code under test | pyspark-testing | Patch 19: packaging scripts are skipped |
| 4 | Imports from the repository root (`from tests.job1 import ...`) failed | databricks-template | Patch 19: project root added with the lowest priority |
| 5 | A leftover `PYSPARK_PYTHON` started Spark workers on another Python | example_pyspark_app | Patch 20: workers always use the driver's Python |
| 6 | Tests ran from the tool's work folder, breaking relative paths (`data/sample.csv`) | example_pyspark_app | Patch 21: tests run from the project folder |
| 7 | The determinism template compared objects without value equality (`ArgumentParser`) | databricks-template | Patch 22: compare `repr` for such objects |
| 8 | Generated tests from an earlier run into the same folder were run again and escaped quarantine | databricks-template | Patch 22/23: repeated quarantine; old files removed at the start of a run |
| 9 | Mode C missed division by zero under ANSI | databricks-medallion-pipeline | Patch 24: rule `P-ANSI-DIV` (review only) |
| 10 | Writing the generated `conftest.py` crashed when the project and the output folder were on different Windows drives (no relative path between drives) | GitHub Actions Windows runner (repository on D:, temporary folders on C:) | Patch 30: absolute path when no relative path exists |

### Continuous integration
The unit tests run on GitHub Actions on Ubuntu and Windows with Python 3.11 and 3.12 (`.github/workflows/tests.yml`); failing tests are shown as annotations on the run. The Windows runner found defect 10 above. It also showed that two tests relied on the `patch` program, whose Windows build on the runner (GNU patch 2.5.9 from Strawberry Perl) crashes on valid patches; those tests now check the cleanup patch with `git apply --check`.

### Limitations shown by this evaluation
- Coverage targets were not reached on the larger repository (19.9-20.4% against 80%): 56 functions were skipped (entry points,
  parameters that need cloud services or file fixtures), and template tests mostly execute code rather than check results.
- Notebooks written as linear scripts (no functions) get no generated tests; only Mode C applies to them.
- Results depend heavily on the local environment. Most of the effort on the real repositories went into dependency pins
  (Python, NumPy, pandas, PyArrow, Delta), which also illustrates the problem Mode C addresses.
- Some code needs a newer Spark than is available locally (`pyspark.pipelines`, Spark 4.1) and cannot be tested on PySpark 3.5.
- All labels were written by the tool's author.

## Still planned
- Mutation score (mutmut) across all generated tests, offline versus LLM, instead of a single injected bug.
- Labels by someone other than the rule author for Mode C.
- Run model proposals under the emulated target runtime and report whether they pass, instead of leaving them unverified.
- A repository with real dead code to measure Mode B dead-code recall.
