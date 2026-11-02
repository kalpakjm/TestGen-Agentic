"""Behaviour plugins: local emulation and test patterns for changes that can be tested on a laptop.

The release notes decide WHICH plugins activate (a rule that says a Spark option now defaults to a new
value). A plugin only supplies the code patterns and the test template for that option. Adding a plugin
for another option means adding one entry here.
"""
from __future__ import annotations

ANSI_CONF = "spark.sql.ansi.enabled"


def ansi_rule(trigger: dict) -> dict:
    return {
        "id": "P-ANSI",
        "title": "ANSI-sensitive casts and date parsing",
        "description": "With ANSI mode on, a value that cannot be cast or parsed raises an error instead of becoming null.",
        "matchers": [
            {"type": "cast_to", "types": ["int", "integer", "long", "bigint", "short", "smallint", "float", "double",
                                          "decimal", "date", "timestamp"]},
            {"type": "dotted_suffix", "names": ["to_date", "to_timestamp"]},
        ],
        "applies": {"type": "always"},
        "severity": "high",
        "recommendation": "Validate input before casting, use try_cast through F.expr for dirty data, or set "
                          "spark.sql.ansi.enabled to false explicitly.",
        "source": trigger.get("source", ""),
        "origin": "plugin:ansi",
        "plugin": "ansi",
        "verified": bool(trigger.get("verified")),
        "evidence": f"Activated by rule {trigger.get('id')}: {trigger.get('title', '')[:100]}",
    }


def ansi_division_rule(trigger: dict) -> dict:
    """Division of Spark columns: with ANSI mode on, dividing by zero raises instead of returning null.
    Kept as a separate rule (review only): the migrated-test template feeds unparseable text, which does not
    exercise a zero divisor."""
    return {
        "id": "P-ANSI-DIV",
        "title": "ANSI-sensitive division of columns (division by zero raises)",
        "description": "With ANSI mode on, dividing a column by zero raises DIVIDE_BY_ZERO instead of returning null.",
        "matchers": [{"type": "column_division"}],
        "applies": {"type": "always"},
        "severity": "high",
        "recommendation": "Guard the divisor (when(divisor != 0, a / divisor)), use try_divide through F.expr, "
                          "or set spark.sql.ansi.enabled to false explicitly.",
        "source": trigger.get("source", ""),
        "origin": "plugin:ansi",
        "plugin": "ansi-review",
        "verified": bool(trigger.get("verified")),
        "evidence": f"Activated by rule {trigger.get('id')}: {trigger.get('title', '')[:100]}",
    }


def plugin_rules(rules: list) -> list:
    """Extra detection rules for the plugins that the release-note rules activate."""
    trigger = next((r for r in rules if r.get("plugin") == "ansi"), None)
    if trigger is None:
        trigger = next((r for r in rules if (r.get("emulation") or {}).get("conf") == ANSI_CONF
                        and (r.get("emulation") or {}).get("value") == "true"), None)
    if trigger is None:
        return []
    present = {r.get("id") for r in rules}
    extra = []
    if not any(r.get("plugin") == "ansi" for r in rules):
        extra.append(ansi_rule(trigger))
    if "P-ANSI-DIV" not in present:
        extra.append(ansi_division_rule(trigger))
    return extra
