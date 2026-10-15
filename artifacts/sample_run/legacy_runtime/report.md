# ptagent report: legacy_runtime

- Mode: **C** (requested: C)
- LLM backend: offline-template
- Files: 3 source, 2 notebooks, 0 test files, 6 functions

## Runtime impact (DBR 13.3 to 17.3)

- Findings: 16 in 4 files
- Tests to review: 0

  - R008 notebooks/legacy_daily_job.ipynb cell1:L1: Legacy Spark SQL configuration flags
  - R007 notebooks/legacy_daily_job.ipynb cell2:L1: DBFS mounts
  - R007 notebooks/legacy_daily_job.ipynb cell3:L3: DBFS mounts
  - R002 notebooks/legacy_daily_job.ipynb cell3:L4: ANSI SQL mode enabled by default (Spark 4.0)
  - R006 notebooks/legacy_report.py L10: RDD API usage
  - R001 notebooks/legacy_report.py L14: Modules removed in Python 3.12 (distutils, imp)
  - R004 src/legacy_etl/loaders.py L12: SQLContext is deprecated
  - R003 src/legacy_etl/loaders.py L14: DataFrame.registerTempTable is deprecated
  - R001 src/legacy_etl/loaders.py L2: Modules removed in Python 3.12 (distutils, imp)
  - R002 src/legacy_etl/loaders.py L20: ANSI SQL mode enabled by default (Spark 4.0)
  - R002 src/legacy_etl/loaders.py L21: ANSI SQL mode enabled by default (Spark 4.0)
  - R006 src/legacy_etl/loaders.py L26: RDD API usage
  - R004 src/legacy_etl/loaders.py L3: SQLContext is deprecated
  - R007 src/legacy_etl/loaders.py L30: DBFS mounts
  - R005 src/legacy_etl/transforms.py L2: PandasUDFType is deprecated
  - R005 src/legacy_etl/transforms.py L5: PandasUDFType is deprecated

