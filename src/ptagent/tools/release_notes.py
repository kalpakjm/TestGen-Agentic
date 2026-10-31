"""Fetch and parse official release notes (Databricks Runtime pages and Spark migration guides).

Pages are fetched with the standard library, cached on disk, and turned into a list of sections
(heading path, paragraphs, list items). Local files (.html, .md, .txt) can be supplied instead for
offline use. Nothing here depends on the exact wording of a page, only on its heading structure.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional

from .versions import parse_version

ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\ufeff]")
SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "table", "noscript", "svg", "form", "button"}


class ReleaseNotesError(Exception):
    """A page could not be fetched (network, timeout, server error)."""


@dataclass
class Section:
    path: tuple
    level: int
    paragraphs: list = field(default_factory=list)
    items: list = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.path[-1] if self.path else ""

    @property
    def text(self) -> str:
        return " ".join(self.paragraphs + self.items)


def clean(text: str) -> str:
    text = ZERO_WIDTH.sub("", text).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------- HTML parsing
class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections = [Section((), 0)]
        self.stack = []
        self.skip = 0
        self.heading_level = None
        self.heading_buf = ""
        self.mode = None
        self.buf = ""
        self.links = []

    # -- helpers
    def _flush(self):
        text = clean(self.buf)
        toc = self.mode == "li" and self.links and all(h.startswith("#") for h in self.links)
        if text and not toc:
            (self.sections[-1].paragraphs if self.mode == "p" else self.sections[-1].items).append(text)
        self.buf, self.mode, self.links = "", None, []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if re.fullmatch(r"h[1-4]", tag):
            self._flush()
            self.heading_level, self.heading_buf = int(tag[1]), ""
        elif tag == "p":
            self._flush()
            self.mode = "p"
        elif tag == "li":
            self._flush()
            self.mode = "li"
        elif tag == "code":
            self._add("`")
        elif tag == "a" and self.mode == "li":
            self.links.append(dict(attrs).get("href") or "")
        elif tag == "br":
            self._add(" ")

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if self.heading_level is not None and tag == f"h{self.heading_level}":
            text = clean(self.heading_buf)
            level = self.heading_level
            self.heading_level = None
            if text:
                self.stack = [h for h in self.stack if h[0] < level] + [(level, text)]
                self.sections.append(Section(tuple(t for _, t in self.stack), level))
        elif tag in ("p", "li"):
            self._flush()
        elif tag == "code":
            self._add("`")

    def _add(self, text):
        if self.heading_level is not None:
            self.heading_buf += text
        elif self.mode:
            self.buf += text

    def handle_data(self, data):
        if not self.skip:
            self._add(data)


def parse_html(html: str) -> list:
    parser = _Parser()
    parser.feed(html)
    parser._flush()
    return [s for s in parser.sections if s.path]


# ---------------------------------------------------------------------- Markdown / text parsing
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_TOC_BULLET = re.compile(r"^\[[^\]]+\]\(#[^)]*\)\s*$")


def parse_markdown(text: str) -> list:
    sections, stack, paragraph = [Section((), 0)], [], []

    def flush():
        joined = clean(_LINK.sub(r"\1", " ".join(paragraph)).replace("**", ""))
        if joined:
            sections[-1].paragraphs.append(joined)
        paragraph.clear()

    for raw in text.splitlines():
        line = ZERO_WIDTH.sub("", raw).rstrip()
        heading = re.match(r"^(#{1,4})\s+(.*?)\s*$", line)
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        if heading:
            flush()
            level, title = len(heading.group(1)), clean(_LINK.sub(r"\1", heading.group(2)).replace("**", ""))
            stack = [h for h in stack if h[0] < level] + [(level, title)]
            sections.append(Section(tuple(t for _, t in stack), level))
        elif bullet:
            flush()
            body = bullet.group(1)
            if not _TOC_BULLET.match(body.strip()):
                item = clean(_LINK.sub(r"\1", body).replace("**", ""))
                if item:
                    sections[-1].items.append(item)
        elif not line.strip():
            flush()
        elif not line.lstrip().startswith(("|", "---", "←", "✕")):
            paragraph.append(line)
    flush()
    return [s for s in sections if s.path]


def parse_document(text: str) -> list:
    head = text[:3000].lower()
    return parse_html(text) if ("<html" in head or "<h1" in text[:20000].lower() or "<body" in head) else parse_markdown(text)


# ---------------------------------------------------------------------- fetching
def default_opener(url: str, timeout: int):
    request = urllib.request.Request(url, headers={"User-Agent": "ptagent/0.1 (release notes reader)", "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 404, ""
        raise ReleaseNotesError(f"HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise ReleaseNotesError(f"cannot reach {url}: {exc}") from exc


class Fetcher:
    """Local files first, then the on-disk cache, then the network."""

    def __init__(self, cache_dir, cache_days=30, timeout=30, offline=False, refresh=False, local_dir=None,
                 opener: Optional[Callable] = None):
        self.cache_dir = Path(cache_dir)
        self.cache_days = cache_days
        self.timeout = timeout
        self.offline = offline
        self.refresh = refresh
        self.local_dir = Path(local_dir) if local_dir else None
        self.opener = opener or default_opener

    def _local(self, names):
        if not self.local_dir:
            return None
        for name in names:
            for ext in (".html", ".md", ".txt"):
                path = self.local_dir / f"{name}{ext}"
                if path.exists():
                    return path.read_text(encoding="utf-8")
        return None

    def get(self, url: str, local_names=()):
        """Return (text or None, origin) where origin is local, cache, network or missing."""
        text = self._local(local_names)
        if text is not None:
            return text, "local"
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        body, meta = self.cache_dir / f"{key}.html", self.cache_dir / f"{key}.json"
        if meta.exists() and not self.refresh:
            info = json.loads(meta.read_text(encoding="utf-8"))
            age_days = (time.time() - info.get("fetched_at", 0)) / 86400
            if info.get("status") == 404 and age_days < 1:
                return None, "cache"
            if info.get("status") == 200 and body.exists() and (self.offline or age_days <= self.cache_days):
                return body.read_text(encoding="utf-8"), "cache"
        if self.offline:
            return None, "missing"
        status, text = self.opener(url, self.timeout)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        meta.write_text(json.dumps({"url": url, "status": status, "fetched_at": time.time()}), encoding="utf-8")
        if status == 200:
            body.write_text(text, encoding="utf-8")
            return text, "network"
        return None, "missing"


# ---------------------------------------------------------------------- reading one runtime page
ENV_LINE = re.compile(r"^(Operating System|Java|Scala|Python|R|Delta Lake)\s*:\s*(.+)$", re.I)
SPARK_PATTERNS = [  # tried in this order, over the first sections of the page
    re.compile(r"(?:powered by|based on|built on|includes|with)\s+Apache Spark\s+(\d+\.\d+(?:\.\d+)?)", re.I),
    re.compile(r"Apache Spark\s+(\d+\.\d+(?:\.\d+)?)", re.I),
    re.compile(r"\bSpark\s+(\d+\.\d+(?:\.\d+)?)", re.I),
]
CHANGES_HEADING = re.compile(r"behaviou?r(al)? changes?|breaking changes?", re.I)


@dataclass
class Change:
    source: str          # databricks-release-notes | spark-migration-guide
    version: str         # Databricks Runtime version, or Spark version for guide items
    title: str
    text: str
    url: str
    breaking: bool = False


@dataclass
class VersionNotes:
    version: str
    url: str
    origin: str
    spark: str = ""
    environment: dict = field(default_factory=dict)
    changes: list = field(default_factory=list)


def find_spark_version(sections: list) -> str:
    """Spark version a runtime page states, trying the most reliable wording first."""
    first, rest = SPARK_PATTERNS[0], SPARK_PATTERNS[1]
    attempts = [
        (first, sections),                                       # 'powered by / includes Apache Spark X' anywhere
        (rest, [s for s in sections if s.level <= 2]),           # 'Apache Spark X' in a top-level section
        (rest, sections),                                        # 'Apache Spark X' anywhere
        (SPARK_PATTERNS[2], sections[:10]),                      # bare 'Spark X', first sections only
    ]
    for pattern, scope in attempts:
        for section in scope:
            match = pattern.search(" ".join([section.title, section.text]))
            if match:
                return match.group(1)
    return ""


def read_runtime_page(version: str, url: str, origin: str, sections: list) -> VersionNotes:
    notes = VersionNotes(version=version, url=url, origin=origin)
    notes.spark = find_spark_version(sections)
    for section in sections:
        if any(re.search(r"system environment", p, re.I) for p in section.path) and len(section.path) <= 3:
            for item in section.items:
                env = ENV_LINE.match(item.replace("**", ""))
                if env:
                    notes.environment[env.group(1).lower().replace(" ", "_")] = env.group(2).strip()
    for index, section in enumerate(sections):
        idx = next((i for i, p in enumerate(section.path) if CHANGES_HEADING.search(p)), None)
        if idx is None:
            continue
        depth = len(section.path) - idx
        if depth >= 2:  # a sub-heading under 'Behavioral changes' is one change
            text = section.text
            title = section.title
            notes.changes.append(Change("databricks-release-notes", version, title, text, url,
                                        breaking="breaking change" in (title + " " + text).lower()))
        elif depth == 1:  # bullets directly under the heading (older page layout)
            for item in section.items:
                notes.changes.append(Change("databricks-release-notes", version, item.split(". ")[0][:120], item, url,
                                            breaking="breaking change" in item.lower()))
    return notes


# ---------------------------------------------------------------------- finding the pages between two versions
def probe_runtime_pages(fetcher: Fetcher, base_url: str, frm: tuple, to: tuple, max_gap: int = 2):
    """Return ([(version tuple, url, origin, text)], warnings) for every runtime page from `frm` up to `to`.

    Versions are discovered by trying major.minor and major.minorlts, so no version list is needed.
    The target version is always tried, even after a gap.
    """
    found, warnings = [], []

    def attempt(version):
        label = f"{version[0]}.{version[1]}"
        for suffix in ("", "lts"):
            url = f"{base_url}{label}{suffix}"
            try:
                text, origin = fetcher.get(url, local_names=(f"{label}{suffix}", label))
            except ReleaseNotesError as exc:
                warnings.append(str(exc))
                continue
            if text is not None:
                return (version, url, origin, text)
        return None

    for major in range(frm[0], to[0] + 1):
        misses, hits = 0, 0
        for minor in range(0, 40):
            version = (major, minor)
            if version < frm:
                continue
            if version > to:
                break
            result = attempt(version)
            if result:
                found.append(result)
                misses, hits = 0, hits + 1
            else:
                misses += 1
                if misses >= max_gap and (hits or minor >= max_gap):
                    break
        if major == to[0] and not any(v == to for v, *_ in found):
            result = attempt(to)
            if result:
                found.append(result)
        if not any(v[0] == major for v, *_ in found):  # newer majors publish one page per major release
            url = f"{base_url}{major}"
            try:
                text, origin = fetcher.get(url, local_names=(str(major), f"{major}.0"))
            except ReleaseNotesError as exc:
                warnings.append(str(exc))
                text, origin = None, "error"
            if text is not None:
                found.append(((major, 0), url, origin, text))
    found.sort(key=lambda item: item[0])
    return found, warnings


# ---------------------------------------------------------------------- Spark migration guides
UPGRADE_HEADING = re.compile(r"Upgrading from (?:PySpark|Spark SQL|Spark Core|Spark) (\d+\.\d+)(?:\.\d+)? to (\d+\.\d+)", re.I)


def read_spark_guide(sections: list, url: str, spark_from: tuple, spark_to: tuple) -> list:
    """Items of every 'Upgrading from X to Y' section that lies inside the Spark version range."""
    changes = []
    for section in sections:
        for part in section.path:
            match = UPGRADE_HEADING.search(part)
            if not match:
                continue
            lower, upper = parse_version(match.group(1)), parse_version(match.group(2))
            if lower >= spark_from and upper <= spark_to:
                for item in section.items:
                    changes.append(Change("spark-migration-guide", match.group(2), item.split(". ")[0][:120], item, url,
                                          breaking="removed" in item.lower()))
            break
    return changes
