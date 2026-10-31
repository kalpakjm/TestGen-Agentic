"""End-to-end orders pipeline."""
import argparse

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

from orders_etl.aggregations import customer_lifetime_value, daily_revenue, top_customers
from orders_etl.cleaning import add_order_total, clean_orders, flag_high_value
from orders_etl.storage import read_orders, write_parquet
from orders_etl.utils import safe_divide


def run_pipeline(spark: SparkSession, orders_path: str, output_path: str) -> float:
    """Clean orders, write three Parquet outputs and return the average order value."""
    raw = read_orders(spark, orders_path)
    orders = flag_high_value(add_order_total(clean_orders(raw)))
    write_parquet(daily_revenue(orders), f"{output_path}/daily_revenue")
    write_parquet(top_customers(orders), f"{output_path}/top_customers")
    write_parquet(customer_lifetime_value(orders), f"{output_path}/customer_lifetime_value")
    totals = orders.agg(F.sum("order_total").alias("total"), F.count("order_id").alias("n")).collect()[0]
    return safe_divide(float(totals["total"] or 0.0), float(totals["n"]))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the orders pipeline")
    parser.add_argument("--orders", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    spark = SparkSession.builder.appName("orders_etl").getOrCreate()
    print(run_pipeline(spark, args.orders, args.output))


if __name__ == "__main__":
    main()
