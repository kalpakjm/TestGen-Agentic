"""Turn parsed release notes into detection rules.

Three sources feed the rules, all derived from what the pages say for the chosen upgrade:
  * environment differences (Python, Scala, Java, Spark versions) between the two runtimes;
  * every 'Behavioral changes' entry of every runtime in between;
  * every item of the Spark migration guides for the Spark versions the upgrade crosses.
A change becomes a code-matching rule only when it names something that can be found in code
(a function, an option, a class). Everything else is kept as an advisory for a human to read.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .versions import fmt, parse_version

CODE = re.compile(r"`([^`\n]{2,90})`")
SPLIT_AT = re.compile(r"\b(use|using|instead|replace[sd]?\s+(?:by|with)|set|migrate|migrating|switch|recommends?)\b", re.I)
SIGNALS = [
    ("high", r"\bremoved\b|\bno longer\b|end-of-life|not allowed|prohibited|not supported|unsupported|dropped"),
    ("medium", r"deprecated|\braises?\b|\bthrows?\b|\bfails?\b|disallow|renamed|default|now (?:enabled|disabled)|changed|disabled|blocked|now requires?|incompatible"),
]
GENERIC = {
    "append", "items", "count", "format", "load", "save", "read", "write", "show", "collect", "filter", "select",
    "join", "agg", "cast", "sum", "max", "min", "first", "last", "drop", "get", "set", "map", "keys", "values",
    "head", "tail", "sort", "mode", "name", "type", "copy", "apply", "update", "index", "value", "data", "none",
    "true", "false", "null", "auto", "string", "default", "table", "query", "path", "column", "columns",
}
CONCEPT_CONFS = {"ansi": "spark.sql.ansi.enabled"}


def _signal(text: str):
    lowered = text.lower()
    for severity, pattern in SIGNALS:
        if re.search(pattern, lowered):
            return severity
    return None


PYTHON_PACKAGES = ("pyspark.", "pandas.", "numpy.", "pyarrow.", "delta.")
NOT_A_BREAKING_CHANGE = re.compile(r"^(?:improved?|fix(?:ed|es)?|new|added|adds?|support for|enhanced?)\b", re.I)


def _classify(token: str):
    token = token.strip().rstrip("()").strip()
    last_part = token.split(".")[-1]
    if re.search(r"(?:Exception|Error)$", last_part):
        return None  # error types are what gets raised, not what the code calls
    if last_part in ("MaxValue", "MinValue") or re.fullmatch(r"[A-Z][a-z]+\.(?:MaxValue|MinValue)", token):
        return None  # Java/Scala constants
    if token.startswith(PYTHON_PACKAGES):
        last = token.split(".")[-1]
        return ("dotted", "." + last) if len(last) >= 5 and last.lower() not in GENERIC else None
    if re.fullmatch(r"(?:spark|cloudFiles)\.[\w.\-]+", token):
        return "conf", token
    if re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z][A-Za-z0-9_\-]*){2,}", token):
        return "conf", token  # lowercase dotted name with 3+ parts is an option key, such as kafka.sasl.client.x
    if re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", token):
        last = token.split(".")[-1]
        return ("dotted", "." + last) if len(last) >= 5 and last.lower() not in GENERIC else None
    if re.fullmatch(r"_?[a-z]+(?:_[a-z0-9]+)+", token) and token.lower() not in GENERIC:
        return "func", token
    if re.fullmatch(r"[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]+)+", token):
        return "class", token
    return None


def _emulation(text: str, confs: list):
    lowered = text.lower()
    for conf in confs:
        if not conf.startswith("spark.") or conf.startswith("spark.databricks."):
            continue
        near = lowered
        if re.search(r"(?:enabled|on|true) by default|now enabled|turned on by default|default (?:value )?(?:is|to) `?true", near):
            return {"conf": conf, "value": "true"}
        if re.search(r"(?:disabled|off|false) by default|now disabled|turned off by default|default (?:value )?(?:is|to) `?false", near):
            return {"conf": conf, "value": "false"}
        changed = re.search(r"changed from `?\w+`? to `?(true|false)`?", near)
        if changed:
            return {"conf": conf, "value": changed.group(1)}
    if "ansi" in lowered and re.search(r"by default|now (?:enabled|on)", lowered) and not re.search(r"disabled by default|off by default", lowered):
        return {"conf": CONCEPT_CONFS["ansi"], "value": "true"}
    return None


def _recommendation(text: str, url: str) -> str:
    match = re.search(r"((?:Use|Replace|Set|Migrate|Switch|To preserve)[^.]{5,200}\.)", text)
    if match:
        return match.group(1).strip()
    return f"Review the change in the release notes: {url}"


def rule_from_change(change, rule_id: str, introduced_in: str):
    """Build a rule (or None when the change is not about anything findable or testable)."""
    text = change.text
    if NOT_A_BREAKING_CHANGE.match(change.title.strip()) and not change.breaking:
        return None
    subject = SPLIT_AT.split(text, maxsplit=1)[0] if SPLIT_AT.search(text) else text
    severity = _signal(change.title + " " + subject)
    if change.breaking:
        severity = "high"
    all_codes = CODE.findall(change.title) + CODE.findall(text)
    subject_codes = list(dict.fromkeys(CODE.findall(change.title) + CODE.findall(subject)))
    emulation = _emulation(change.title + " " + text, [c for c in all_codes if _classify(c) and _classify(c)[0] == "conf"])
    matchers = []
    if severity:
        kinds = [k for k in (_classify(t) for t in subject_codes[:8]) if k]
        if any(k[0] == "conf" for k in kinds):
            kinds = [k for k in kinds if k[0] == "conf"]  # a named option is the subject; functions are context
        for kind in dict.fromkeys(kinds):
            if kind[0] == "conf":
                matchers.append({"type": "string_contains", "substrings": [kind[1]]})
            elif kind[0] in ("dotted", "func"):
                matchers.append({"type": "dotted_suffix", "names": [kind[1]]})
            else:
                matchers += [{"type": "name", "names": [kind[1]]}, {"type": "import_from_name", "names": [kind[1]]}]
    if not matchers and not emulation:
        return None
    return {
        "id": rule_id,
        "title": change.title[:110],
        "description": text[:300],
        "matchers": matchers,
        "applies": {"type": "upgrade_range", "introduced_in": introduced_in},
        "severity": severity or "medium",
        "recommendation": _recommendation(text, change.url),
        "source": change.url,
        "origin": change.source,
        "verified": True,
        "evidence": f"Stated in {change.source} for version {change.version}.",
        **({"emulation": emulation} if emulation else {}),
    }


def _python_minor(value: str):
    numbers = re.findall(r"\d+", value or "")
    return (int(numbers[0]), int(numbers[1])) if len(numbers) >= 2 else None


def environment_rules(brief, removals: dict) -> list:
    """Rules from what changed in Python, Scala and the JVM between the two runtimes."""
    rules = []
    first = brief.versions[0] if brief.versions else None
    last = brief.versions[-1] if brief.versions else None
    if not first or not last:
        return rules
    py_from, py_to = _python_minor(first.environment.get("python", "")), _python_minor(last.environment.get("python", ""))
    if py_from and py_to and py_to > py_from:
        for key, entry in sorted(removals.get("versions", {}).items(), key=lambda kv: parse_version(kv[0])):
            target = parse_version(key)
            if not (py_from < target <= py_to):
                continue
            introduced = next((v.version for v in brief.versions if (_python_minor(v.environment.get("python", "")) or (0, 0)) >= target), last.version)
            matchers = []
            if entry.get("imports"):
                matchers.append({"type": "import", "names": entry["imports"]})
            for item in entry.get("from_imports", []):
                matchers.append({"type": "import_from", "module": item["module"], "names": item["names"]})
            if entry.get("attributes"):
                matchers.append({"type": "dotted_suffix", "names": entry["attributes"]})
            rules.append({
                "id": f"PY{key}", "title": f"Removed in Python {key}",
                "description": f"The runtime moves from Python {fmt(py_from)} to {fmt(py_to)} (from the release notes). Python {key} removed these modules and functions.",
                "matchers": matchers, "applies": {"type": "upgrade_range", "introduced_in": introduced},
                "severity": "high", "recommendation": f"Replace the removed modules and functions; see https://docs.python.org/3/whatsnew/{key}.html",
                "source": last.url, "origin": "environment", "verified": True,
                "evidence": f"Python {first.environment.get('python')} -> {last.environment.get('python')} in the system environment sections."})
    scala_from = re.match(r"(\d+\.\d+)", first.environment.get("scala", ""))
    scala_to = re.match(r"(\d+\.\d+)", last.environment.get("scala", ""))
    if scala_from and scala_to and scala_from.group(1) != scala_to.group(1):
        introduced = next((v.version for v in brief.versions if (v.environment.get("scala", "")).startswith(scala_to.group(1))), last.version)
        rules.append({
            "id": "ENV-scala", "title": f"Scala {scala_from.group(1)} replaced by Scala {scala_to.group(1)}",
            "description": f"Libraries built for Scala {scala_from.group(1)} (artifact names ending in _{scala_from.group(1)}) cannot be loaded on Scala {scala_to.group(1)}.",
            "matchers": [{"type": "string_contains", "substrings": [f"_{scala_from.group(1)}"]}],
            "applies": {"type": "upgrade_range", "introduced_in": introduced}, "severity": "high",
            "recommendation": f"Use the _{scala_to.group(1)} build of each library and test the job on the new runtime.",
            "source": last.url, "origin": "environment", "verified": True,
            "evidence": f"Scala {first.environment.get('scala')} -> {last.environment.get('scala')} in the system environment sections."})
    return rules


def jvm_review_items(brief) -> list:
    first = brief.versions[0] if brief.versions else None
    last = brief.versions[-1] if brief.versions else None
    if not first or not last:
        return []
    java_from, java_to = first.environment.get("java", ""), last.environment.get("java", "")
    major = lambda text: (re.search(r"(?:zulu|jdk|java)?\D*(\d{1,2})[.\-+]", text.lower()) or [None, None])[1]
    if java_from and java_to and major(java_from) != major(java_to):
        return [{"rule": "ENV-java", "title": f"Java changes from {java_from} to {java_to}",
                 "files": [], "action": "Java and Scala libraries on the cluster must be compatible with the new JDK.",
                 "note": "Not detectable in Python code; check cluster libraries."}]
    return []


def synthesize(brief, removals_path):
    """Return (rules, advisories)."""
    removals = json.loads(Path(removals_path).read_text(encoding="utf-8")) if removals_path and Path(removals_path).exists() else {}
    rules = environment_rules(brief, removals)
    advisories, seen, counters = [], set(), {}
    def introduced_for_spark(target):
        """First runtime whose Spark version reaches `target`. A runtime with an unknown Spark version
        counts as possibly reaching it, so rules start at the earliest runtime that could have it."""
        candidate = None
        for v in brief.versions:
            spark = _python_minor(v.spark)
            if spark is None:
                candidate = candidate or v.version
                continue
            if spark >= target:
                return candidate or v.version
            candidate = None
        return brief.versions[-1].version if brief.versions else brief.to_version

    baseline = parse_version(brief.from_version)
    changes = [(c, c.version) for v in brief.versions if parse_version(v.version) > baseline for c in v.changes]
    changes += [(c, introduced_for_spark(parse_version(c.version))) for c in brief.guide_changes]
    for change, introduced in changes:
        prefix = "D" if change.source == "databricks-release-notes" else "S"
        counters[prefix] = counters.get(prefix, 0) + 1
        rule = rule_from_change(change, f"{prefix}{change.version}-{counters[prefix]}", introduced)
        if rule is None:
            advisories.append({"source": change.source, "version": change.version, "title": change.title,
                               "breaking": change.breaking, "url": change.url})
            continue
        key = json.dumps([rule["matchers"], rule.get("emulation")], sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        rules.append(rule)
    return rules, advisories
