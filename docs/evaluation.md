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

## Planned evaluation for the final submission
- A larger corpus of real, openly licensed PySpark projects (licences recorded).
- Mutation score (mutmut) to measure whether generated tests detect injected bugs, not just whether they run.
- Offline template versus LLM backend on identical inputs.
- Impact-detection precision/recall on a held-out set labelled by someone other than the rule author, against official release notes.
