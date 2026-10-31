"""Release-notes agent: reads the official notes for the chosen upgrade and derives the rules from them."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..tools.release_notes import (Fetcher, parse_document, probe_runtime_pages, read_runtime_page, read_spark_guide)
from ..tools.rule_synthesis import jvm_review_items, synthesize
from ..tools.versions import fmt, parse_version
from .base import Agent

ENV_KEYS = ("spark", "python", "scala", "java", "operating_system", "delta_lake")


@dataclass
class UpgradeBrief:
    from_version: str
    to_version: str
    versions: list = field(default_factory=list)
    guide_changes: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def env_diff(self) -> dict:
        if not self.versions:
            return {}
        first, last = self.versions[0], self.versions[-1]
        values = lambda v, key: v.spark if key == "spark" else v.environment.get(key, "")
        return {key: {"from": values(first, key), "to": values(last, key)}
                for key in ENV_KEYS if values(first, key) or values(last, key)}

    def summary(self) -> dict:
        return {
            "from": self.from_version, "to": self.to_version,
            "versions": [{"version": v.version, "origin": v.origin, "url": v.url, "spark": v.spark,
                          "environment": v.environment, "behavioral_changes": len(v.changes)} for v in self.versions],
            "environment_changes": self.env_diff(),
            "spark_guide_items": len(self.guide_changes),
            "warnings": self.warnings,
        }


class ReleaseNotesAgent(Agent):
    name = "release_notes"

    def __init__(self, trace, cfg, fetcher=None):
        super().__init__(trace)
        self.cfg = cfg
        self.fetcher = fetcher

    def _fetcher(self, local_dir, offline, refresh):
        if self.fetcher is not None:
            return self.fetcher
        return Fetcher(self.cfg.notes_cache_dir, self.cfg.notes_cache_days, self.cfg.notes_timeout_s,
                       offline=offline, refresh=refresh, local_dir=local_dir)

    def build_brief(self, from_version: str, to_version: str, local_dir=None, offline=False, refresh=False) -> UpgradeBrief:
        frm, to = parse_version(from_version), parse_version(to_version)
        brief = UpgradeBrief(fmt(frm), fmt(to))
        fetcher = self._fetcher(local_dir, offline, refresh)
        pages, warnings = probe_runtime_pages(fetcher, self.cfg.notes_base_url, frm, to)
        brief.warnings += warnings
        for version, url, origin, text in pages:
            notes = read_runtime_page(fmt(version), url, origin, parse_document(text))
            brief.versions.append(notes)
        if not brief.versions:
            brief.warnings.append("No Databricks Runtime release notes could be read for this range "
                                  "(no network access and nothing cached, or the page layout changed).")
        else:
            if brief.versions[0].version != fmt(frm):
                brief.warnings.append(f"No page was found for the starting runtime {fmt(frm)}; "
                                      f"environment changes are measured from {brief.versions[0].version}.")
            known = [v for v in brief.versions if v.spark]
            spark_from = parse_version(known[0].spark) if known else None
            spark_to = parse_version(known[-1].spark) if known else None
            for v in brief.versions:
                if not v.spark:
                    brief.warnings.append(f"The Spark version of runtime {v.version} could not be read from its page.")
            if spark_from and spark_to and spark_to > spark_from:
                for label, url in (("pyspark_upgrade", self.cfg.pyspark_guide_url), ("sql_migration_guide", self.cfg.sql_guide_url)):
                    try:
                        text, origin = fetcher.get(url, local_names=(label,))
                    except Exception as exc:  # network problems must not stop the run
                        brief.warnings.append(str(exc))
                        continue
                    if text is None:
                        brief.warnings.append(f"Spark migration guide not available: {url}")
                        continue
                    brief.guide_changes += read_spark_guide(parse_document(text), url, spark_from, spark_to)
        self.log("release notes read", versions=[v.version for v in brief.versions],
                 behavioral_changes=sum(len(v.changes) for v in brief.versions),
                 spark_guide_items=len(brief.guide_changes), warnings=len(brief.warnings))
        return brief

    def rules(self, brief: UpgradeBrief):
        """Return (rules, advisories, review items for things that cannot be detected in Python code)."""
        rules, advisories = synthesize(brief, self.cfg.python_removals_path)
        self.log("rules derived", rules=len(rules), advisories=len(advisories))
        return rules, advisories, jvm_review_items(brief)
