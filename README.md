# Agentic Test Engineering for PySpark on Databricks (baseline submission)

A multi-agent system that generates and maintains pytest suites for PySpark code written for Databricks (`.py` modules, `.ipynb` notebooks and Databricks-format notebooks). It handles three situations:

| Mode | Trigger | What the agents do |
| --- | --- | --- |
| **A** | The project has no tests | Generate tests, check them, run them, repair or quarantine the ones that fail |
| **B** | The project has tests | Find dead code and stale tests, find functions that lack tests, generate tests for the gaps |
| **C** | A Databricks Runtime (DBR) upgrade is requested | Find the notebooks and modules that the upgrade affects and the tests that need review |

## Business objective
Teams that run PySpark on Databricks often have little or no test coverage, tests that drift away from the code, and an upgrade process that depends on engineers manually finding what a new runtime breaks. The objective is to cut the effort of writing and maintaining tests and to make runtime upgrades safer and faster. Success is measured by coverage gained, first-run pass rate of generated tests, and precision/recall of upgrade-impact detection (see `docs/evaluation.md`).

## How Mode C finds what an upgrade changes
You give two runtime versions. The tool fetches the official release-notes page of every runtime in between (`docs.databricks.com`), plus the Spark migration guides when the Spark version changes, and caches them in `.cache/release_notes`. From those pages it reads the system environment (Python, Scala, Java, Spark), every "Behavioral changes" entry and every migration-guide item, and turns them into rules: removed modules for the Python versions crossed, the Scala build suffix, functions and options named as removed or changed, and options whose default flipped. Notes that name nothing findable in code are listed as advisories. No rule is written by hand for a particular upgrade. Offline use: save pages as `17.3lts.html` or `16.0.md` in a folder and pass `--release-notes-dir`.

Behaviour that can be emulated on a laptop (for example an option that becomes on by default) is tested: existing tests run under the old and new setting, and plugins write tests for the code patterns involved (the baseline has one plugin, for ANSI mode). Everything else is listed for manual review.

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
Requirements: Python 3.11 recommended (it matches DBR 15.4; 3.10 and 3.12 also work), and Java 17 (11 works) for running Spark.
Linux, macOS and WSL need nothing else. Native Windows also needs `winutils.exe` and `hadoop.dll` for Hadoop 3 in `%HADOOP_HOME%\bin`.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows (cmd): .venv\Scripts\activate
pip install -r requirements.txt
```

Or install it as a package, which also adds a `ptagent` command (`ptagent run ...` is the same as `python run.py run ...`):
```bash
pip install -e ".[spark]"          # the tool plus PySpark
pip install -e ".[spark,eval]"     # also the pins needed to run some real projects (see docs/evaluation.md)
```
Use the editable install (`-e`): the tool reads `configs/`, `knowledge/`, `prompts/` and `data/` from this folder.
With a regular install, run it from this folder or set `PTAGENT_HOME` to it.

Do not set `PYSPARK_PYTHON` to another Python: the tool starts Spark's workers with its own interpreter.

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
python run.py run /path/to/project --from-dbr 15.4 --to-dbr 17.3     # also runs Mode C (reads the release notes)
python run.py notes --from-dbr 15.4 --to-dbr 17.3                    # only read the release notes and show the rules derived from them
python run.py analyze /path/to/project                               # index summary only
python run.py run /path/to/project --mode B --fail-under 80          # exit code 1 if final coverage is below 80%
python run.py run /path/to/project --mode B --apply-in-place         # copy the cleanup over your sources (asks first, keeps .bak files)
```

Outputs per project (in `outputs/<project>/`): `report.md`, `report.json`, `run_trace.json` (what each agent did), `generated_tests/`, `extracted/` (functions pulled out of notebooks), `refactored/` (cleaned copies), `cleanup.patch` (a diff you can apply with `git apply` or `patch -p1`) and `cleaned_project/` (a copy used to measure coverage without dead code). Your sources are never modified unless you pass `--apply-in-place` and confirm, `verify_*/` (JUnit and coverage files when tests were executed). A reference run is stored in `artifacts/sample_run/`.

Test execution is automatic: when `pytest`, `pyspark`, `coverage` and Java are present, generated tests are run and coverage is measured. Otherwise the run still completes and reports that execution was skipped.

Optional local LLM (free): install [Ollama](https://ollama.com), run `ollama pull qwen2.5-coder:7b`, then `python run.py demo --backend ollama`.

Optional hosted LLMs (paid by the provider, not needed for the project): `--backend openai | gemini | anthropic`. Set the key in an environment variable first (`set OPENAI_API_KEY=...`, `set GEMINI_API_KEY=...` or `set ANTHROPIC_API_KEY=...` in cmd), and optionally choose a model with `--model`. These backends send your function source code to the provider; the default and Ollama backends do not. Without a key the run falls back to the offline templates and says so in the trace. `openai_base_url` in `configs/config.yaml` also lets the `openai` backend talk to any OpenAI-compatible server.

## Current implementation status
| Component | Status |
| --- | --- |
| Project and notebook analysis (`.py`, `.ipynb`, Databricks `.py`) | Working; unit tested |
| Mode A: template test generation with schema inference, file fixtures for path parameters, verification and quarantine of failing tests | Working; run on real Spark for the sample projects |
| Mode A: LLM-assisted generation and repair (Ollama) | Implemented, not yet evaluated |
| Mode B: dead-code and stale-test detection with confidence levels and protection (decorators, `keep_functions`, `# ptagent: keep`), cleanup patch, coverage verdict against the target | Working; run on real Spark for the sample project |
| Mode B: untested-function detection; line-level gaps from coverage data | Working (line-level gaps need Spark to run) |
| Mode C: reads the official Databricks Runtime release notes and Spark migration guides for the upgrade you give it, derives detection rules, finds impacted modules and notebooks | Working on saved test pages; first run against the live pages still to be checked |
| Mode C: curated rule file for DBR 15.4 to 17.3 (used as a fallback or with `--rules both`) | Working; 13 rules, 7 with sources |
| Mode C: test migration for the ANSI-mode change: runs existing tests under an ANSI emulation, writes migrated tests, lists everything else for manual review | Working offline; the execution part needs Spark and has not yet been run end to end |
| Mode C: optional code migration | Not built |
| Knowledge base and retrieval (guidelines and runtime rules) | Working |
| Evaluation harness on labelled samples | Working |

**Important:** the build environment had no PySpark, so the dynamic parts (running generated tests, measuring coverage, repair loop against real failures) have been written and their parsing is unit tested, but they have **not been run against a real Spark session by the author**. The first `python run.py demo` on a machine with Spark is the first end-to-end test of that path. The evaluation numbers in `artifacts/sample_run/evaluation_report.md` therefore cover only the static parts.

## Known limitations
- Generated tests are structural (return type, empty input, null input, determinism for pure functions). They do not yet assert business results; that is the job of the LLM backend and of the planned improvements.
- Schema inference is heuristic; unusual patterns fall back to string columns and may fail. Failing generated tests are repaired (LLM) or quarantined (offline) and reported.
- Functions with plain string path parameters get file-fixture tests (a temporary CSV is written for inputs, a temporary folder is used for outputs). These tests are template-only, and the input CSV uses the columns found in the project's other functions. Functions that need other fixtures or complex types, and entry points like `main`, are skipped and listed with a reason.
- Dead-code detection is name based. Functions used only through dynamic dispatch, a public API or external callers can be reported as dead. Only functions that nothing references (confidence high) are placed in the cleanup patch; functions that only tests reference (medium) are listed but kept. Protect others with `keep_functions` in `configs/config.yaml` or a `# ptagent: keep` comment on the `def` line. Nothing is deleted automatically.
- Notebook support extracts function definitions only. Cell-level logic and cross-cell globals are not tested.
- The runtime rules in `knowledge/dbr_rules.json` cover the path DBR 15.4 LTS to 17.3 LTS. Rules marked `verified` cite the release note or migration guide they come from (checked on 2026-10-29). The other rules are general deprecation advisories and were not checked against the Databricks release notes. Other upgrade paths need new rules.
- Rules derived from release notes are heuristic: a change becomes a code pattern only when the page names a function, option or class in code formatting. Names that are too generic (`append`, `count`) are never matched. Expect some false positives and misses, and read `generated_rules.json` after a run. The page layout can change, which is why `python run.py notes` shows what was parsed.
- Test migration can emulate only options with a changed default (the baseline has a test-writing plugin for ANSI mode only). Changes such as Python 3.12, Scala 2.13 or removed functions cannot be tested locally and are reported as items to review by hand.
- Reading the live pages needs network access. If none of the pages can be read, the run falls back to the curated rule file and says so in the report.
- The evaluation set is small and written by the same author as the rules, so perfect scores on it show the baseline is wired correctly, not that it generalises.

## Planned improvements for the final submission
See `docs/planned_enhancements.md`. In short: assertion-rich tests with LLM-proposed expected values checked by execution, file and multi-DataFrame fixtures, mutation testing as a quality metric, test migration for Mode C (code migration as an optional add-on), a larger real-world evaluation corpus with licences recorded, and comparison of the offline and LLM backends.
