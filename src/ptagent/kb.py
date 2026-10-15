"""Knowledge preparation and retrieval.

Builds a small searchable index (TF-IDF over chunks) from:
  * knowledge/testing_guidelines.md  - how to test PySpark code
  * knowledge/dbr_rules.json         - Databricks Runtime change rules
Retrieved chunks are injected into LLM prompts and recorded in the run report.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def tokenize(text: str) -> list:
    return [t.lower() for t in TOKEN.findall(text)]


@dataclass
class Chunk:
    id: str
    source: str
    title: str
    text: str


@dataclass
class KnowledgeBase:
    chunks: list = field(default_factory=list)
    _tf: list = field(default_factory=list)
    _idf: dict = field(default_factory=dict)

    @classmethod
    def build(cls, guidelines_path: Path, rules_path: Path) -> "KnowledgeBase":
        kb = cls()
        kb.chunks.extend(load_guideline_chunks(guidelines_path))
        kb.chunks.extend(load_rule_chunks(rules_path))
        kb._index()
        return kb

    def _index(self):
        self._tf = [Counter(tokenize(c.title + " " + c.text)) for c in self.chunks]
        n = max(len(self.chunks), 1)
        df = Counter()
        for tf in self._tf:
            df.update(tf.keys())
        self._idf = {t: math.log((1 + n) / (1 + d)) + 1.0 for t, d in df.items()}

    def search(self, query: str, k: int = 3) -> list:
        q = Counter(tokenize(query))
        scored = []
        for chunk, tf in zip(self.chunks, self._tf):
            norm = math.sqrt(sum((tf[t] * self._idf.get(t, 0)) ** 2 for t in tf)) or 1.0
            score = sum(q[t] * tf[t] * self._idf.get(t, 0) ** 2 for t in q if t in tf) / norm
            if score > 0:
                scored.append((score, chunk))
        scored.sort(key=lambda pair: -pair[0])
        return [chunk for _, chunk in scored[:k]]

    def save(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        data = [{"id": c.id, "source": c.source, "title": c.title, "chars": len(c.text)} for c in self.chunks]
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def load_guideline_chunks(path: Path) -> list:
    path = Path(path)
    if not path.exists():
        return []
    chunks, title, buffer = [], None, []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if title and buffer:
                chunks.append(Chunk(f"guide-{len(chunks) + 1}", "testing_guidelines", title, "\n".join(buffer).strip()))
            title, buffer = line[3:].strip(), []
        elif title is not None:
            buffer.append(line)
    if title and buffer:
        chunks.append(Chunk(f"guide-{len(chunks) + 1}", "testing_guidelines", title, "\n".join(buffer).strip()))
    return chunks


def load_rule_chunks(path: Path) -> list:
    path = Path(path)
    if not path.exists():
        return []
    rules = json.loads(path.read_text(encoding="utf-8")).get("rules", [])
    return [
        Chunk(r["id"], "dbr_rules", r["title"], f"{r['description']} Recommendation: {r['recommendation']}")
        for r in rules
    ]
