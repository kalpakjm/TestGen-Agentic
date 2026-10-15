"""Functions extracted from notebook notebooks/orders_exploration.ipynb by ptagent."""
from pyspark.sql import functions as F


def status_counts(df):
    """Number of orders per status, most frequent first."""
    return df.groupBy("status").count().orderBy(F.col("count").desc())
