# Agentic Test Engineering for PySpark on Databricks (baseline submission)

A multi-agent system that generates and maintains pytest suites for PySpark code written for Databricks (`.py` modules, `.ipynb` notebooks and Databricks-format notebooks). It handles three situations:

| Mode | Trigger | What the agents do |
| --- | --- | --- |
| **A** | The project has no tests | Generate tests, check them, run them, repair or quarantine the ones that fail |
| **B** | The project has tests | Find dead code and stale tests, find functions that lack tests, generate tests for the gaps |
| **C** | A Databricks Runtime (DBR) upgrade is requested | Find the notebooks and modules that the upgrade affects and the tests that need review |

## Business objective
Teams that run PySpark on Databricks often have little or no test coverage, tests that drift away from the code, and an upgrade process that depends on engineers manually finding what a new runtime breaks. The objective is to cut the effort of writing and maintaining tests and to make runtime upgrades safer and faster. Success is measured by coverage gained, first-run pass rate of generated tests, and precision/recall of upgrade-impact detection (see `docs/evaluation.md`).

## Where Generative AI is used (and where it is not)
- **Deterministic components:** code analysis (AST), schema inference, runtime-rule matching, test execution and coverage. These need to be exact and reproducible, so no LLM is used.
- **LLM-assisted components:** writing richer test bodies (`prompts/test_generation.txt`), repairing failing tests (`prompts/test_repair.txt`) and, in the final submission, migrating tests (`prompts/runtime_migration.txt`).
- **Default backend is `offline`:** a deterministic template that needs no model, so the project runs anywhere. Set `llm.backend: ollama` in `configs/config.yaml` (or pass `--backend ollama`) to use a free, local open-weight model. Every LLM answer is validated and replaced by the template if it is unusable.

## Dataset source and licence
All sample code and data were written by the author for this project and are released under the MIT licence (`LICENSE`). They are synthetic and contain no personal data. See `data/README.md` for the file list. No third-party code is included.

## Repository structure
```
.
├── README.md  requirements.txt  pytest.ini  run.py  LICENSE
├── configs/        config.yaml (LLM backend, paths, thresholds, runtime versions)
├── knowledge/      testing_guidelines.md, dbr_rules.json (context for retrieval and rule matching)
├── prompts/        test_generation.txt, test_repair.txt, runtime_migration.txt (planned)
├── src/ptagent/
│   ├── agents/     orchestrator, ingestion, test_generator, verifier, obsolete_code, coverage_gap, runtime_impact
│   ├── tools/      analyzer (AST), notebooks (.ipynb / Databricks .py), columns (schema inference)
│   ├── evaluation/ evaluate.py, metrics.py
│   ├── kb.py       knowledge base (TF-IDF retrieval)
│   ├── llm.py      offline and Ollama backends
│   └── cli.py
├── data/           sample_data/ (CSV), sample_projects/ (3 projects), labels/ (hand-written ground truth)
├── notebooks/      01_baseline_walkthrough.ipynb
├── tests/          unit tests for the framework itself
├── scripts/        prepare_data.py (CSV to Parquet)
├── artifacts/      sample_run/: reports, generated tests and cleaned copies from a real run
├── assets/         architecture.svg
└── docs/           architecture.md, evaluation.md, prompt_design.md, planned_enhancements.md
```

## Installation
Requirements: Python 3.10 or 3.11 (3.12 works with the `setuptools` pin in `requirements.txt`), and Java 11 or 17 (Java 17 recommended) for running Spark. Linux, macOS or WSL are recommended; native Windows needs extra Hadoop setup.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Optional, to produce the Parquet version of the sample data:
```bash
python scripts/prepare_data.py
```

## Execution steps
No file needs editing. From the repository root:

```bash
python run.py demo                  # runs Mode A, B and C on the three sample projects -> outputs/
python run.py evaluate              # baseline evaluation on the labelled samples -> outputs/evaluation_report.md
pytest                              # unit tests of the framework itself
```

Run on your own project:
```bash
python run.py run /path/to/project                                   # auto-selects Mode A or B
python run.py run /path/to/project --from-dbr 13.3 --to-dbr 17.3     # also runs Mode C
python run.py analyze /path/to/project                               # index summary only
```

Outputs per project (in `outputs/<project>/`): `report.md`, `report.json`, `run_trace.json` (what each agent did), `generated_tests/`, `extracted/` (functions pulled out of notebooks), `refactored/` (cleaned copies; your sources are never modified), `verify_*/` (JUnit and coverage files when tests were executed). A reference run is stored in `artifacts/sample_run/`.

Test execution is automatic: when `pytest`, `pyspark`, `coverage` and Java are present, generated tests are run and coverage is measured. Otherwise the run still completes and reports that execution was skipped.

Optional local LLM (free): install [Ollama](https://ollama.com), run `ollama pull qwen2.5-coder:7b`, then `python run.py demo --backend ollama`.

## Current implementation status
| Component | Status |
| --- | --- |
| Project and notebook analysis (`.py`, `.ipynb`, Databricks `.py`) | Working; unit tested |
| Mode A: template test generation with schema inference, verification and quarantine of failing tests | Working; generated files are syntax-checked and unit tested |
| Mode A: LLM-assisted generation and repair (Ollama) | Implemented, not yet evaluated |
| Mode B: dead-code and stale-test detection, cleaned copies | Working; unit tested |
| Mode B: untested-function detection; line-level gaps from coverage data | Working (line-level gaps need Spark to run) |
| Mode C: rule-based impact detection across modules and notebooks, version-range aware | Working; unit tested |
| Mode C: test migration (and optional code migration) | Planned for the final submission |
| Knowledge base and retrieval (guidelines and runtime rules) | Working |
| Evaluation harness on labelled samples | Working |

**Important:** the build environment had no PySpark, so the dynamic parts (running generated tests, measuring coverage, repair loop against real failures) have been written and their parsing is unit tested, but they have **not been run against a real Spark session by the author**. The first `python run.py demo` on a machine with Spark is the first end-to-end test of that path. The evaluation numbers in `artifacts/sample_run/evaluation_report.md` therefore cover only the static parts.

## Known limitations
- Generated tests are structural (return type, empty input, null input, determinism for pure functions). They do not yet assert business results; that is the job of the LLM backend and of the planned improvements.
- Schema inference is heuristic; unusual patterns fall back to string columns and may fail. Failing generated tests are repaired (LLM) or quarantined (offline) and reported.
- Functions that need file paths, multiple fixtures or complex types are skipped and listed with a reason.
- Dead-code detection is name based. Functions used only through dynamic dispatch, a public API or external callers are reported as dead and need a human decision. Nothing is deleted automatically.
- Notebook support extracts function definitions only. Cell-level logic and cross-cell globals are not tested.
- The runtime rules in `knowledge/dbr_rules.json` are an illustrative starter set. The versions at which each rule starts to apply must be checked against the official Databricks release notes before relying on them.
- The evaluation set is small and written by the same author as the rules, so perfect scores on it show the baseline is wired correctly, not that it generalises.

## Planned improvements for the final submission
See `docs/planned_enhancements.md`. In short: assertion-rich tests with LLM-proposed expected values checked by execution, file and multi-DataFrame fixtures, mutation testing as a quality metric, test migration for Mode C (code migration as an optional add-on), a larger real-world evaluation corpus with licences recorded, and comparison of the offline and LLM backends.
