"""Reading and writing helpers."""
from pyspark.sql import DataFrame, SparkSession


def read_orders(spark: SparkSession, path: str) -> DataFrame:
    """Read the raw orders CSV with every column as a string."""
    return spark.read.csv(path, header=True, inferSchema=False)


def write_parquet(df: DataFrame, path: str) -> None:
    """Write a DataFrame as Parquet, replacing existing output."""
    df.write.mode("overwrite").parquet(path)
