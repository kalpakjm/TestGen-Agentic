# ptagent report: orders_with_tests

- Mode: **B** (requested: B)
- LLM backend: offline-template
- Files: 6 source, 0 notebooks, 3 test files, 14 functions

## Test generation

- Functions targeted: 8
- Tests generated: 14 in 3 files
- Skipped functions: 3
  - `orders_etl.pipeline.run_pipeline`: parameter 'orders_path' needs a file fixture (planned for the final submission)
  - `orders_etl.storage.read_orders`: parameter 'path' needs a file fixture (planned for the final submission)
  - `orders_etl.storage.write_parquet`: parameter 'path' needs a file fixture (planned for the final submission)
- Static check: 3/3 files valid
- Execution: 17 passed, 0 failed, 1 errors; coverage 43.8%

## Obsolete code

- Dead function `orders_etl.cleaning.legacy_normalize_status` (src/orders_etl/cleaning.py:35-37)
- Dead function `orders_etl.utils.normalize_country` (src/orders_etl/utils.py:11-13)
- Dead function `orders_etl.utils.is_weekend` (src/orders_etl/utils.py:16-18)
- Stale test import `legacy_compute_tax` from `orders_etl.cleaning` in tests/test_legacy_tax.py

## Coverage gaps

- Functions without tests: 8

- Coverage before: 15.6, after: 43.8, target: 80.0

