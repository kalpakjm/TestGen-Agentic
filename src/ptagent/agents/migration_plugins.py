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


def plugin_rules(rules: list) -> list:
    """Extra detection rules for the plugins that the release-note rules activate."""
    if any(r.get("plugin") == "ansi" for r in rules):
        return []
    for rule in rules:
        emulation = rule.get("emulation")
        if emulation and emulation.get("conf") == ANSI_CONF and emulation.get("value") == "true":
            return [ansi_rule(rule)]
    return []
