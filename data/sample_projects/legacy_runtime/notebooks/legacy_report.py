# Databricks notebook source
# MAGIC %md
# MAGIC # Legacy report

# COMMAND ----------

from pyspark.sql import functions as F

orders = spark.table("main.sales.orders")
sample = orders.rdd.map(lambda row: row.amount).take(5)

# COMMAND ----------

import imp

print(imp.__name__, len(sample))
