# Baseline evaluation

LLM backend: offline

## Mode A: test generation (orders_no_tests)

- Eligibility precision/recall: 1.0 / 1.0
- Tests generated: 30; files valid: 4/4
- Skipped functions as expected: True
- Execution: {'status': 'skipped', 'reason': 'missing: pytest, pyspark, coverage (install requirements.txt and a JDK to enable test execution)'}

## Mode B: maintenance (orders_with_tests)

- Dead functions P/R: 1.0 / 1.0
- Stale test imports P/R: 1.0 / 1.0
- Untested functions P/R: 1.0 / 1.0
- Coverage: {'baseline_percent': None, 'after_percent': None, 'target_percent': 80.0}
- Execution: {'status': 'skipped', 'reason': 'missing: pytest, pyspark, coverage (install requirements.txt and a JDK to enable test execution)'}

## Mode C: runtime impact (legacy_runtime, DBR 13.3 to 17.3)

- Findings P/R: 1.0 / 1.0
- Impacted files P/R: 1.0 / 1.0
