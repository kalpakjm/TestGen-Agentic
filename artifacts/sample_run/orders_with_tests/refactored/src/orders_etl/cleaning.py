"""Cleaning transformations for the orders dataset."""
from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F


def clean_orders(df: DataFrame) -> DataFrame:
    """Trim text, cast types, drop rows without an order id and keep the latest record per order."""
    typed = (
        df.withColumn("order_id", F.trim(F.col("order_id")))
        .withColumn("customer_id", F.trim(F.col("customer_id")))
        .withColumn("quantity", F.col("quantity").cast("int"))
        .withColumn("unit_price", F.col("unit_price").cast("double"))
        .withColumn("order_date", F.to_date(F.col("order_date")))
        .withColumn("status", F.lower(F.trim(F.col("status"))))
        .filter(F.col("order_id").isNotNull())
    )
    latest_first = Window.partitionBy("order_id").orderBy(F.col("updated_at").desc())
    return (
        typed.withColumn("_rn", F.row_number().over(latest_first))
        .filter(F.col("_rn") == 1)
        .drop("_rn")
    )


def add_order_total(df: DataFrame) -> DataFrame:
    """Add order_total = quantity * unit_price."""
    return df.withColumn("order_total", F.col("quantity") * F.col("unit_price"))


def flag_high_value(df: DataFrame, threshold: float = 500.0) -> DataFrame:
    """Flag orders whose total reaches the threshold."""
    return df.withColumn("is_high_value", F.col("order_total") >= threshold)
