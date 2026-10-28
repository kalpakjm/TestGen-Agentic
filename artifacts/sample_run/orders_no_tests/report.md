# ptagent report: orders_no_tests

- Mode: **A** (requested: A)
- LLM backend: offline-template
- Files: 6 source, 1 notebooks, 0 test files, 15 functions

## Test generation

- Functions targeted: 15
- Tests generated: 30 in 4 files
- Skipped functions: 4
  - `orders_etl.pipeline.run_pipeline`: parameter 'orders_path' needs a file fixture (planned for the final submission)
  - `orders_etl.pipeline.main`: entry point (needs an integration test)
  - `orders_etl.storage.read_orders`: parameter 'path' needs a file fixture (planned for the final submission)
  - `orders_etl.storage.write_parquet`: parameter 'path' needs a file fixture (planned for the final submission)
- Static check: 4/4 files valid
- Execution: 30 passed, 0 failed, 0 errors; coverage 48.4%

