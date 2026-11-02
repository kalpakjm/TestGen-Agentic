# Agentic Test Engineering for PySpark on Databricks

[![unit tests](https://github.com/kalpakjm/TestGen-Agentic/actions/workflows/tests.yml/badge.svg)](https://github.com/kalpakjm/TestGen-Agentic/actions/workflows/tests.yml)

A multi-agent system that generates, maintains and migrates pytest suites for PySpark code written for Databricks (`.py` modules, `.ipynb` notebooks and Databricks-format `.py` notebooks). It handles three situations:

| Mode | Trigger | What the agents do |
| --- | --- | --- |
| **A** | The project has no tests | Generate tests, check them, run them on Spark, repair or quarantine the ones that fail |
| **B** | The project has tests | Find dead code and stale tests, find untested functions, generate tests for the gaps, measure coverage before and after, and produce a cleanup patch |
| **C** | A Databricks Runtime (DBR) upgrade is requested | Read the official release notes for the upgrade, find the code and tests it affects, run the existing tests under the new behaviour, and write migrated tests |

Code migration (rewriting the project's own code for the new runtime) is out of scope; Mode C migrates and reviews **tests**.

## Business objective
Teams that run PySpark on Databricks often have little or no test coverage, tests that drift away from the code, and an upgrade process that depends on engineers reading release notes and finding by hand what a new runtime breaks. The objective is to cut the effort of writing and maintaining tests and to make runtime upgrades safer. Success is measured by coverage gained, the pass rate of generated tests, whether generated tests catch injected bugs, and the precision and recall of upgrade-impact detection (see `docs/evaluation.md`).

## Results in brief
Measured on Windows 11 (8 GB RAM, no GPU), Python 3.11, PySpark 3.5.9, Java 17. Full details, labels and method: `docs/evaluation.md`.

| Project | Mode | Result |
| --- | --- | --- |
| `orders_no_tests` (sample) | A | Offline templates: 33 tests, all pass, 84.4% coverage. Local LLM: 59 tests, all pass, 87.5% |
| `orders_with_tests` (sample) | B | Coverage 15.6% with the existing tests, 82.8% after generated tests and cleanup (target 80%: PASS) |
| [databricks-template](https://github.com/andre-salvati/databricks-template) (Apache-2.0) | B | Coverage 11.1% -> 19.9% (offline) / 20.4% (LLM); 0 failing generated tests |
| [example_pyspark_app](https://github.com/guillesd/example_pyspark_app) | B | 84.4% with the project's own tests; target met |
| [databricks-medallion-pipeline](https://github.com/ahsan-habib-sunny/databricks-medallion-pipeline) (MIT) | C, DBR 15.4 -> 17.3 | 20 findings, 0 false alarms; precision 65-100% depending on the data |

- **Injected bug:** with `safe_divide` changed from division to multiplication, an LLM-generated test failed and caught it; the offline templates check only types and cannot.
- **Ten tool defects** were found by running on real repositories and on GitHub's Windows runners, and fixed. None showed up on the sample projects.

## How Mode C finds what an upgrade changes
You give two runtime versions. The tool fetches the official release-notes page of every runtime in between (`docs.databricks.com`), plus the Spark migration guides when the Spark version changes, and caches them in `.cache/release_notes`. From those pages it reads the system environment (Python, Scala, Java, Spark), every "Behavioral changes" entry and every migration-guide item, and turns them into rules: removed modules for the Python versions crossed, the Scala build suffix, functions and options named as removed or changed, and options whose default flipped. Notes that name nothing findable in code are listed as advisories. No rule is written by hand for a particular upgrade. `ptagent notes --from-dbr 15.4 --to-dbr 17.3` shows what was read and derived. Offline use: save pages as `17.3lts.html` or `16.0.md` in a folder and pass `--release-notes-dir`.

Behaviour that can be reproduced on a laptop is tested. For an option whose default flips (ANSI mode is on by default from Spark 4.0 / DBR 17.x), the existing tests run once with the old setting and once with the new one, which shows exactly which tests the upgrade breaks, and a plugin writes migrated tests for the affected functions. Column division, which raises on a zero divisor under ANSI, is flagged for review. Everything that cannot be reproduced locally (Python 3.12, Scala 2.13, Java 17, removed functions) is listed for manual review.

## Where Generative AI is used (and where it is not)
- **Deterministic components:** code analysis (AST), schema inference, release-note parsing and rule derivation, rule matching, test execution, coverage and the cleanup patch. These need to be exact and reproducible, so no LLM is used.
- **LLM-assisted components:** richer test bodies with concrete expected values (`prompts/test_generation.txt`, `prompts/test_generation_pure.txt`), repairing failing tests (`prompts/test_repair.txt`) and proposing rewrites of tests the upgrade breaks (`prompts/runtime_migration.txt`).
- **Every model answer is checked:** it must parse, use only known names and fixtures, and pass when run. Answers that fail are repaired, then quarantined, so a bad answer never ends up in the suite. Migration proposals are saved as UNVERIFIED and never run automatically.
- **Backends:** `offline` (default; deterministic templates, no model), `ollama` (free, local open-weight model; the code never leaves your machine), and optional hosted APIs `openai`, `gemini`, `anthropic` (paid by the provider; they receive your function source code).

## Dataset source and licence
- **Sample projects and data** (`data/`): written by the author for this project, synthetic, no personal data, released under the MIT licence (`LICENSE`).
- **Evaluation repositories**: four public GitHub repositories, cloned for evaluation only and **not included** in this repository. Links, licences and what each was used for are in `docs/evaluation.md`. One of them has no licence, so only its metrics are reported.

## Repository structure
```
.
├── README.md  LICENSE  pyproject.toml  requirements.txt  pytest.ini  run.py
├── .github/workflows/tests.yml   unit tests on Ubuntu and Windows, Python 3.11 and 3.12
├── configs/        config.yaml (LLM backend, paths, thresholds, runtime versions, release-note options)
├── knowledge/      testing_guidelines.md, dbr_rules.json (curated fallback rules), python_removals.json
├── prompts/        test_generation.txt, test_generation_pure.txt, test_repair.txt, runtime_migration.txt
├── src/ptagent/
│   ├── agents/     orchestrator, ingestion, test_generator, verifier, obsolete_code, coverage_gap,
│   │               runtime_impact, release_notes, migration, migration_plugins
│   ├── tools/      analyzer (AST), notebooks, columns (schema inference), release_notes (fetch and parse),
│   │               rule_synthesis (notes -> rules), versions
│   ├── evaluation/ evaluate.py, metrics.py
│   ├── kb.py       knowledge base (TF-IDF retrieval)
│   ├── llm.py      offline, Ollama, OpenAI, Gemini and Anthropic backends
│   ├── preflight.py checks before a run (paths, versions, model availability)
│   └── cli.py
├── data/           sample_data/ (CSV), sample_projects/ (4 projects), labels/ (hand-written ground truth)
├── notebooks/      01_baseline_walkthrough.ipynb
├── tests/          unit tests for the framework itself (no Spark or LLM needed)
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
pip install -e ".[spark]"          # the tool, the `ptagent` command and PySpark
```
- `pip install -e ".[spark,eval]"` also installs the version pins some real projects need (NumPy 1.x, PyArrow < 18, pandas < 3, Delta); `docs/evaluation.md` explains each one.
- `pip install -r requirements.txt` with `python run.py ...` instead of `ptagent ...` also works.
- Use the editable install (`-e`): the tool reads `configs/`, `knowledge/`, `prompts/` and `data/` from this folder. With a regular install, run it from this folder or set `PTAGENT_HOME`.
- Do not set `PYSPARK_PYTHON` to another Python: the tool starts Spark's workers with its own interpreter.

Optional local LLM (free): install [Ollama](https://ollama.com), then pull a coding model and name it in `configs/config.yaml` (`llm.ollama_model`):
```bash
ollama pull qwen2.5-coder:3b       # fits in 8 GB RAM; use qwen2.5-coder:7b with 16 GB or more
```
If Ollama is not running or the model is missing, the run says so once and continues with the offline templates.

Optional hosted LLMs (paid by the provider): set `OPENAI_API_KEY`, `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` and pass `--backend openai | gemini | anthropic` (optionally `--model <name>`). `openai_base_url` in the config points the `openai` backend at any OpenAI-compatible server.

## Execution steps
No file needs editing. From the repository root:

```bash
ptagent demo                        # Modes A, B and C on the four sample projects -> outputs/
ptagent demo --backend ollama       # the same with the local LLM
ptagent evaluate                    # evaluation on the labelled samples -> outputs/evaluation_report.md
python -m pytest -q                 # unit tests of the framework itself
```

Run on your own project:
```bash
ptagent run /path/to/project                                   # auto-selects Mode A or B
ptagent run /path/to/project --from-dbr 15.4 --to-dbr 17.3     # also runs Mode C (reads the release notes)
ptagent notes --from-dbr 15.4 --to-dbr 17.3                    # only read the release notes and show the derived rules
ptagent analyze /path/to/project                               # index summary only
ptagent run /path/to/project --mode B --fail-under 80          # exit code 1 if final coverage is below 80%
ptagent run /path/to/project --mode B --apply-in-place         # copy the cleanup over your sources (asks first, keeps .bak files)
ptagent --debug run /path/to/project                           # full traceback if something fails
```

Outputs per project (in `outputs/<project>/`, or `--output-dir`):
- `report.md` and `report.json`: what was found and the results; `run_trace.json`: what each agent did, step by step.
- `generated_tests/`: the generated tests and their `conftest.py`; `quarantined_tests/`: tests removed because they kept failing, with the failure message.
- `extracted/`: functions pulled out of notebooks; `verify_*/`: JUnit and coverage files from each test run.
- Mode B: `refactored/`, `cleanup.patch` (apply with `git apply`) and `cleaned_project/` (used to measure coverage without dead code).
- Mode C: `upgrade_brief.json`, `generated_rules.json`, `migrated_tests/` (and `migrated_tests/proposals/` for unverified model rewrites).

Your sources are never modified unless you pass `--apply-in-place` and confirm. Test execution is automatic when `pytest`, `pyspark`, `coverage` and Java are present; otherwise the run completes and reports that execution was skipped. A reference run is stored in `artifacts/sample_run/`.

## Implementation status
| Component | Status |
| --- | --- |
| Project and notebook analysis (`.py`, `.ipynb`, Databricks `.py`); code outside `src/`; packaging scripts ignored | Working; tested on 4 real repositories |
| Mode A: template generation with schema inference and file fixtures; execution on Spark; repeated quarantine of failing tests | Working |
| Mode A: LLM generation and repair (Ollama) with validation | Working; evaluated against the templates |
| Mode B: dead code, stale tests, coverage gaps, cleanup patch, coverage verdict | Working; run on the sample project and 3 real repositories |
| Mode C: release notes read live, rules derived per upgrade (20 rules and 87 advisories for DBR 15.4 -> 17.3) | Working; checked against the live pages |
| Mode C: ANSI emulation, broken-test detection, migrated tests, division review rule, unverified LLM proposals | Working |
| Mode C: code migration | Out of scope |
| Hosted LLM backends (OpenAI, Gemini, Anthropic) | Implemented and unit tested with simulated responses; not evaluated |
| Packaging (`pyproject.toml`, `ptagent` command), CI on Ubuntu and Windows, checks before a run | Working |

## Known limitations
- **Template tests are structural:** they check types, empty and null input and determinism, and raise coverage by running code more than by checking results. Assertions on values come from the LLM backend, whose quality depends on the model (the 3B model often gets expected values wrong; those tests are repaired or quarantined).
- **Coverage on large projects stays low:** functions that need cloud services, complex fixtures or are entry points are skipped with a reason (56 of 92 functions on databricks-template).
- **Notebooks written as linear scripts** (no functions) get no generated tests; only Mode C applies to them.
- **Dead-code detection is name based:** functions used through dynamic dispatch or by external callers can be reported as dead. Only unreferenced functions go into the cleanup patch; protect others with `keep_functions` or `# ptagent: keep`. No real repository in the evaluation had dead code, so this part is evaluated only on the sample project.
- **Rules derived from release notes are heuristic:** a change becomes a code pattern only when the page names a function, option or class in code formatting. Expect some false positives and misses; read `generated_rules.json` and `ptagent notes`.
- **Test migration covers options with a changed default** (one plugin, for ANSI mode). Python, Scala and Java changes and removed functions are listed for review, not tested.
- **The local environment matters:** real projects often need specific package versions (NumPy, pandas, PyArrow, Delta). Some code needs a newer Spark than PySpark 3.5 (for example `pyspark.pipelines`, Spark 4.1) and cannot be tested locally.
- **Labels were written by the tool's author**, so precision and recall should be read as indicative.

## Future work
Run model proposals under the emulated runtime and report whether they pass; mutation testing across all generated tests; independent labelling of Mode C findings; more emulation plugins; and a repository with real dead code for Mode B recall. See `docs/planned_enhancements.md`.
