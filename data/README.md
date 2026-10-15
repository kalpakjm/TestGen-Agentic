# Data

All data in this repository was written by the project author for this project and is released under the repository's MIT licence. It contains no personal data: names are invented and order values are synthetic.

| Path | What it is |
| --- | --- |
| `sample_data/orders.csv` | 27 synthetic order rows, including a newer version of an existing order, a blank order id and a row with dirty values. |
| `sample_data/customers.csv` | 6 synthetic customers. |
| `sample_data/parquet/` | Created on demand by `python scripts/prepare_data.py` (needs PySpark and Java). Not shipped, because the build environment had no Spark. |
| `sample_projects/orders_no_tests/` | PySpark code with **no tests** (Mode A input): 5 modules and 1 notebook. |
| `sample_projects/orders_with_tests/` | The same code with a **partial test suite**, three unused helper functions and one stale test (Mode B input). |
| `sample_projects/legacy_runtime/` | Code and notebooks written for an old runtime (Mode C input): 2 modules, 1 `.ipynb` notebook and 1 Databricks-format `.py` notebook. |
| `labels/*.json` | Hand-written ground truth used by the evaluation. Written from reading the sample code, not produced by the agents. |

## Using your own code
The agents accept any folder: `python run.py run /path/to/project`. Before using third-party repositories, check their licences (permissive licences such as MIT or Apache-2.0 allow this use with attribution; a repository without a licence does not).
