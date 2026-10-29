"""Plain data containers shared by the agents."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class ParamInfo:
    name: str
    annotation: Optional[str] = None
    default: Optional[str] = None
    kind: str = "positional"  # positional | kwonly | vararg | kwarg

    @property
    def has_default(self) -> bool:
        return self.default is not None


@dataclass
class FunctionInfo:
    name: str
    module: str
    file: str  # path relative to the project root
    lineno: int
    end_lineno: int
    params: list
    returns: Optional[str]
    docstring: Optional[str]
    source: str
    origin: str = "module"  # module | notebook
    cell: Optional[int] = None
    decorators: list = field(default_factory=list)
    keep: bool = False  # marked with '# ptagent: keep'

    @property
    def qualname(self) -> str:
        return f"{self.module}.{self.name}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("source", None)
        return data


@dataclass
class ProjectIndex:
    root: Path
    name: str
    source_roots: list
    source_files: list = field(default_factory=list)
    test_files: list = field(default_factory=list)
    notebooks: list = field(default_factory=list)
    functions: list = field(default_factory=list)
    test_functions: list = field(default_factory=list)
    code_references: set = field(default_factory=set)
    test_references: set = field(default_factory=set)
    test_imports: list = field(default_factory=list)  # (test file, module, imported name or "")
    trees: dict = field(default_factory=dict)  # rel path -> list of (cell index or None, ast tree)
    warnings: list = field(default_factory=list)

    def rel(self, path) -> str:
        try:
            return Path(path).resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError:
            return Path(path).as_posix()

    @property
    def has_tests(self) -> bool:
        return len(self.test_functions) > 0

    def summary(self) -> dict:
        return {
            "project": self.name,
            "source_files": len(self.source_files),
            "notebooks": len(self.notebooks),
            "test_files": len(self.test_files),
            "functions": len(self.functions),
            "existing_test_functions": len(self.test_functions),
            "warnings": self.warnings,
        }


@dataclass
class FunctionTests:
    function: str
    constants: str
    tests: str
    used_llm: bool = False
    note: str = ""
    rejected_text: str = ""


@dataclass
class GeneratedModule:
    target_module: str
    path: Path
    header: str
    blocks: list = field(default_factory=list)

    def render(self) -> str:
        parts = [self.header]
        for block in self.blocks:
            if block.constants.strip():
                parts.append(block.constants.rstrip() + "\n")
            parts.append(block.tests.rstrip() + "\n")
        return "\n\n".join(parts).rstrip() + "\n"


@dataclass
class Finding:
    rule_id: str
    title: str
    file: str
    location: str  # "L12" for python files, "cell3:L2" for notebooks
    snippet: str
    severity: str
    recommendation: str
    applies: str = ""
