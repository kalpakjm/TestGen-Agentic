"""Business aggregations on cleaned orders."""
from pyspark.sql import DataFrame, Window
from pyspark.sql import functions as F


def daily_revenue(df: DataFrame) -> DataFrame:
    """Revenue and distinct order count per day."""
    return (
        df.groupBy("order_date")
        .agg(F.sum("order_total").alias("revenue"), F.countDistinct("order_id").alias("orders"))
        .orderBy("order_date")
    )


def top_customers(df: DataFrame, n: int = 3) -> DataFrame:
    """The n customers with the highest revenue (dense rank, ties share a rank)."""
    revenue = df.groupBy("customer_id").agg(F.sum("order_total").alias("revenue"))
    ranking = Window.orderBy(F.col("revenue").desc())
    return (
        revenue.withColumn("rank", F.dense_rank().over(ranking))
        .filter(F.col("rank") <= n)
        .orderBy("rank")
    )


def customer_lifetime_value(df: DataFrame) -> DataFrame:
    """Lifetime value, order count and average order value per customer."""
    totals = df.groupBy("customer_id").agg(
        F.sum("order_total").alias("lifetime_value"),
        F.count("order_id").alias("order_count"),
    )
    return totals.withColumn("avg_order_value", F.col("lifetime_value") / F.col("order_count"))
