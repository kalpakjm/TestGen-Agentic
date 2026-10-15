"""Infer the input columns (and rough types) a PySpark function expects, from its AST.

The result is used to build small synthetic DataFrames for generated tests. It is a heuristic:
anything it cannot infer falls back to a string column, and the verifier catches the rest.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RANK = {"string": 0, "numeric_string": 1, "numeric": 2, "date": 3}

NUMERIC_AGGS = {"sum", "avg", "mean", "stddev", "stddev_samp", "stddev_pop", "variance"}
OTHER_AGGS = {"min", "max", "count", "countDistinct", "first", "last", "collect_list", "collect_set"}
COLUMN_ARG_METHODS = {
    "groupBy", "groupby", "select", "drop", "orderBy", "sort", "dropDuplicates",
    "partitionBy", "sortWithinPartitions", "withColumnRenamed",
}
DATE_FIRST_ARG = {"to_date", "to_timestamp", "year", "month", "dayofmonth", "dayofweek",
                  "date_format", "quarter", "weekofyear"}
NUMERIC_TYPES = {"int", "integer", "long", "bigint", "short", "smallint", "float", "double", "decimal"}
DATE_TYPES = {"date", "timestamp"}

SAMPLE_VALUES = {
    "string": ["a", "b"],
    "numeric_string": ["10", "20"],
    "numeric": [10.0, 20.0],
    "date": ["2024-01-15", "2024-01-16"],
}


@dataclass
class ColumnInference:
    columns: dict = field(default_factory=dict)  # name -> kind
    outputs: set = field(default_factory=set)


def _fname(call: ast.Call):
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _col_name(node):
    """Column name if node is col("x") / F.col("x") / df["x"]."""
    if isinstance(node, ast.Call) and _fname(node) in {"col", "column"} and node.args:
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and IDENT.match(arg.value):
            return arg.value
    if isinstance(node, ast.Subscript):
        sl = node.slice
        if isinstance(sl, ast.Constant) and isinstance(sl.value, str) and IDENT.match(sl.value):
            return sl.value
    return None


def _is_number(node, numeric_params):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.operand, ast.Constant):
        return _is_number(node.operand, numeric_params)
    return isinstance(node, ast.Name) and node.id in numeric_params


class _Visitor(ast.NodeVisitor):
    def __init__(self, numeric_params):
        self.kinds = {}
        self.outputs = set()
        self.numeric_params = numeric_params

    def add(self, name, kind="string"):
        if not IDENT.match(name):
            return
        if RANK[kind] >= RANK.get(self.kinds.get(name, "string"), 0):
            self.kinds[name] = kind
        self.kinds.setdefault(name, kind)

    def _add_string_args(self, args):
        for arg in args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                self.add(arg.value)
            elif isinstance(arg, (ast.List, ast.Tuple)):
                self._add_string_args(arg.elts)

    def visit_Call(self, node):
        name = _fname(node)
        col = _col_name(node)
        if col:
            self.add(col)
        if name in COLUMN_ARG_METHODS:
            self._add_string_args(node.args)
        if name == "withColumn" and node.args and isinstance(node.args[0], ast.Constant):
            self.outputs.add(str(node.args[0].value))
        if name in NUMERIC_AGGS | OTHER_AGGS and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                self.add(arg.value, "numeric" if name in NUMERIC_AGGS else "string")
        if name in DATE_FIRST_ARG and node.args:
            first = node.args[0]
            target = first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else _col_name(first)
            if target:
                self.add(target, "date")
        if name == "datediff":
            for arg in node.args[:2]:
                target = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else _col_name(arg)
                if target:
                    self.add(target, "date")
        if name == "cast" and isinstance(node.func, ast.Attribute) and node.args:
            target = _col_name(node.func.value)
            type_arg = node.args[0]
            if target and isinstance(type_arg, ast.Constant) and isinstance(type_arg.value, str):
                type_name = type_arg.value.lower().split("(")[0]
                if type_name in NUMERIC_TYPES:
                    self.add(target, "numeric_string")
                elif type_name in DATE_TYPES:
                    self.add(target, "date")
        self.generic_visit(node)

    def visit_Subscript(self, node):
        col = _col_name(node)
        if col:
            self.add(col)
        self.generic_visit(node)

    def visit_BinOp(self, node):
        left, right = _col_name(node.left), _col_name(node.right)
        always_numeric = isinstance(node.op, (ast.Mult, ast.Sub, ast.Div, ast.Mod, ast.Pow))
        for col, other in ((left, node.right), (right, node.left)):
            if col and (always_numeric or _is_number(other, self.numeric_params)):
                self.add(col, "numeric")
        self.generic_visit(node)

    def visit_Compare(self, node):
        if len(node.comparators) == 1:
            left, right = node.left, node.comparators[0]
            for col, other in ((_col_name(left), right), (_col_name(right), left)):
                if col and _is_number(other, self.numeric_params):
                    self.add(col, "numeric")
        self.generic_visit(node)


def infer_columns(fn_source: str, numeric_params=()) -> ColumnInference:
    """Infer columns from the source of one function. Falls back to a single string column."""
    try:
        tree = ast.parse(fn_source)
    except SyntaxError:
        tree = None
    visitor = _Visitor(set(numeric_params))
    if tree is not None:
        visitor.visit(tree)
    columns = dict(visitor.kinds) or {"value": "string"}
    return ColumnInference(columns=columns, outputs=visitor.outputs)
