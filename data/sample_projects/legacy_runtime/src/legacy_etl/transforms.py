"""Pandas UDFs written with the deprecated PandasUDFType API."""
from pyspark.sql.functions import PandasUDFType, pandas_udf


@pandas_udf("double", PandasUDFType.SCALAR)
def to_pounds(kilograms):
    return kilograms * 2.2046
