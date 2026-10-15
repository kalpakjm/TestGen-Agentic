"""Static analysis of Python sources and notebooks (AST based, no code is executed)."""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Optional

from ..models import FunctionInfo, ParamInfo, ProjectIndex
from .notebooks import is_databricks_source, load_ipynb_cells, sanitize_cell

TEST_DIR_NAMES = {"tests", "test"}


def parse_source(source: str) -> Optional[ast.AST]:
    try:
        return ast.parse(source)
    except SyntaxError:
        return None


def _unparse(node) -> Optional[str]:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive
        return None


def extract_params(args: ast.arguments) -> list:
    params = []
    positional = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for arg, default in zip(positional, defaults):
        params.append(ParamInfo(arg.arg, _unparse(arg.annotation), _unparse(default), "positional"))
    if args.vararg:
        params.append(ParamInfo(args.vararg.arg, _unparse(args.vararg.annotation), None, "vararg"))
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        params.append(ParamInfo(arg.arg, _unparse(arg.annotation), _unparse(default), "kwonly"))
    if args.kwarg:
        params.append(ParamInfo(args.kwarg.arg, _unparse(args.kwarg.annotation), None, "kwarg"))
    return params


def extract_functions(tree, source, module, file, origin="module", cell=None) -> list:
    """Top-level function definitions of a parsed module or notebook cell."""
    lines = source.splitlines()
    found = []
    for node in getattr(tree, "body", []):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = getattr(node, "end_lineno", node.lineno)
        found.append(
            FunctionInfo(
                name=node.name,
                module=module,
                file=file,
                lineno=start,
                end_lineno=end,
                params=extract_params(node.args),
                returns=_unparse(node.returns),
                docstring=ast.get_docstring(node),
                source="\n".join(lines[start - 1 : end]),
                origin=origin,
                cell=cell,
            )
        )
    return found


def collect_references(tree) -> set:
    """Names that code *uses* (calls, attribute access, imports, __all__ entries)."""
    refs = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            refs.add(node.id)
        elif isinstance(node, ast.Attribute):
            refs.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                refs.add(alias.name.split(".")[-1])
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                for const in ast.walk(node.value):
                    if isinstance(const, ast.Constant) and isinstance(const.value, str):
                        refs.add(const.value)
    return refs


def collect_imports(tree) -> list:
    """List of (module, imported_name) pairs. imported_name is '' for plain 'import x'."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, ""))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                found.append((node.module, alias.name))
    return found


def module_name_for(path: Path, roots: list) -> str:
    """Dotted module name of a file relative to the first matching source root."""
    path = Path(path).resolve()
    for root in roots:
        try:
            rel = path.relative_to(Path(root).resolve())
        except ValueError:
            continue
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts) if parts else path.stem
    return path.stem


def notebook_module_name(path: Path) -> str:
    return "nb_" + re.sub(r"\W+", "_", Path(path).stem).strip("_").lower()


def is_test_path(path: Path, root: Path) -> bool:
    name = path.name
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return True
    try:
        parts = path.resolve().relative_to(root.resolve()).parts[:-1]
    except ValueError:
        parts = path.parts[:-1]
    return any(p in TEST_DIR_NAMES for p in parts)


def build_index(project_dir, skip_dirs=None) -> ProjectIndex:
    """Scan a project folder and build the index every agent works from."""
    root = Path(project_dir).resolve()
    skip = set(skip_dirs or [])
    roots = [root / "src"] if (root / "src").is_dir() else [root]
    index = ProjectIndex(root=root, name=root.name, source_roots=roots)

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in skip and not d.startswith("."))
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            rel = index.rel(path)
            if filename.endswith(".ipynb"):
                _index_ipynb(index, path, rel)
            elif filename.endswith(".py"):
                if is_test_path(path, root):
                    _index_test(index, path, rel)
                elif is_databricks_source(path):
                    index.notebooks.append(path)
                    _index_python(index, path, rel, origin="module")
                else:
                    index.source_files.append(path)
                    _index_python(index, path, rel, origin="module")
    return index


def _index_python(index, path, rel, origin):
    source = path.read_text(encoding="utf-8")
    tree = parse_source(source)
    if tree is None:
        index.warnings.append(f"{rel}: could not be parsed (syntax error), skipped")
        return
    index.trees[rel] = [(None, tree)]
    module = module_name_for(path, index.source_roots)
    index.functions.extend(extract_functions(tree, source, module, rel, origin))
    index.code_references |= collect_references(tree)


def _index_ipynb(index, path, rel):
    index.notebooks.append(path)
    module = notebook_module_name(path)
    try:
        cells = load_ipynb_cells(path)
    except (ValueError, OSError) as exc:
        index.warnings.append(f"{rel}: unreadable notebook ({exc})")
        return
    parsed = []
    for cell in cells:
        source = sanitize_cell(cell["source"])
        if source is None:
            continue
        tree = parse_source(source)
        if tree is None:
            index.warnings.append(f"{rel}: cell {cell['index']} could not be parsed, skipped")
            continue
        parsed.append((cell["index"], tree))
        index.functions.extend(
            extract_functions(tree, source, module, rel, origin="notebook", cell=cell["index"])
        )
        index.code_references |= collect_references(tree)
    index.trees[rel] = parsed


def _index_test(index, path, rel):
    index.test_files.append(path)
    tree = parse_source(path.read_text(encoding="utf-8"))
    if tree is None:
        index.warnings.append(f"{rel}: test file could not be parsed")
        return
    index.trees[rel] = [(None, tree)]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            index.test_functions.append(f"{rel}::{node.name}")
    index.test_references |= collect_references(tree)
    for module, name in collect_imports(tree):
        index.test_imports.append((rel, module, name))


def notebook_extract_source(index: ProjectIndex, nb_rel: str) -> str:
    """Build an importable module from the functions (and imports) defined in a notebook."""
    imports, bodies = [], []
    for cell_index, tree in index.trees.get(nb_rel, []):
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                text = ast.unparse(node)
                if text not in imports:
                    imports.append(text)
    for fn in index.functions:
        if fn.file == nb_rel and fn.origin == "notebook":
            bodies.append(fn.source)
    header = f'"""Functions extracted from notebook {nb_rel} by ptagent."""'
    return header + "\n" + "\n".join(imports) + "\n\n\n" + "\n\n\n".join(bodies) + "\n"
