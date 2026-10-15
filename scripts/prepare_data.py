#!/usr/bin/env python3
"""Convert the sample CSV files to Parquet (needs pyspark and Java; see README)."""
from pathlib import Path

from pyspark.sql import SparkSession

DATA = Path(__file__).resolve().parents[1] / "data" / "sample_data"


def main() -> None:
    spark = SparkSession.builder.master("local[1]").appName("prepare-data").config("spark.ui.enabled", "false").getOrCreate()
    for name in ("orders", "customers"):
        df = spark.read.csv(str(DATA / f"{name}.csv"), header=True)
        df.write.mode("overwrite").parquet(str(DATA / "parquet" / name))
        print(f"wrote {DATA / 'parquet' / name} ({df.count()} rows)")
    spark.stop()


if __name__ == "__main__":
    main()
