"""Legacy loaders written for an older Databricks Runtime."""
from distutils.version import LooseVersion
from pyspark.sql import SQLContext
from pyspark.sql import functions as F


def spark_is_new_enough(version: str) -> bool:
    return LooseVersion(version) >= LooseVersion("3.0.0")


def load_orders(sc, path):
    sql_context = SQLContext(sc)
    df = sql_context.read.csv(path, header=True)
    df.registerTempTable("orders_raw")
    return df


def parse_amounts(df):
    return (
        df.withColumn("amount", F.col("amount").cast("int"))
        .withColumn("order_date", F.to_date(F.col("order_date"), "yyyy-MM-dd"))
    )


def count_by_partition(df):
    return df.rdd.mapPartitions(lambda rows: [sum(1 for _ in rows)]).collect()


def read_raw_orders(spark):
    return spark.read.parquet("/dbfs/mnt/raw/orders")
