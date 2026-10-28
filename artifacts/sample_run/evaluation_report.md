# Baseline evaluation

LLM backend: offline

## Mode A: test generation (orders_no_tests)

- Eligibility precision/recall: 1.0 / 1.0
- Tests generated: 30; files valid: 4/4
- Skipped functions as expected: True
- Execution: {'status': 'ran', 'total': 30, 'passed': 30, 'failed': 0, 'errors': 0, 'coverage_percent': 48.4}

## Mode B: maintenance (orders_with_tests)

- Dead functions P/R: 1.0 / 1.0
- Stale test imports P/R: 1.0 / 1.0
- Untested functions P/R: 1.0 / 1.0
- Coverage: {'baseline_percent': 15.6, 'after_percent': 43.8, 'target_percent': 80.0}
- Execution: {'status': 'ran', 'total': 18, 'passed': 17, 'failed': 0, 'errors': 1, 'coverage_percent': 43.8}

## Mode C: runtime impact (legacy_runtime, DBR 13.3 to 17.3)

- Findings P/R: 1.0 / 1.0
- Impacted files P/R: 1.0 / 1.0
