import json
import tempfile
import unittest
from pathlib import Path

from helpers import REPO, SAMPLES

from ptagent.agents.base import Trace
from ptagent.agents.migration import emulation_confs, emulation_env
from ptagent.agents.migration_plugins import plugin_rules
from ptagent.agents.orchestrator import Orchestrator
from ptagent.agents.release_notes import ReleaseNotesAgent
from ptagent.agents.runtime_impact import matches
from ptagent.config import load_config
from ptagent.tools.release_notes import (Fetcher, ReleaseNotesError, parse_html, parse_markdown, probe_runtime_pages,
                                         read_runtime_page, read_spark_guide)
from ptagent.tools.rule_synthesis import rule_from_change
from ptagent.tools.release_notes import Change
import ast

FIXTURES = REPO / "tests" / "fixtures" / "release_notes"


def offline_agent(**overrides):
    cfg = load_config()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg, ReleaseNotesAgent(Trace(), cfg, Fetcher(Path(tempfile.mkdtemp()), offline=True, local_dir=FIXTURES))


class ParserTests(unittest.TestCase):
    def test_html_sections_skip_navigation_tables_and_contents_lists(self):
        sections = parse_html((FIXTURES / "17.3lts.html").read_text(encoding="utf-8"))
        titles = [s.title for s in sections]
        self.assertIn("Behavioral changes", titles)
        everything = " ".join(s.text for s in sections)
        self.assertNotIn("Other page", everything)   # nav
        self.assertNotIn("Footer link", everything)  # footer
        self.assertNotIn("2.2.3", everything)        # table
        behavioral = next(s for s in sections if s.title == "Behavioral changes")
        self.assertEqual(behavioral.items, [])        # the contents list of anchors is dropped

    def test_page_reading_extracts_environment_spark_and_changes(self):
        sections = parse_html((FIXTURES / "17.3lts.html").read_text(encoding="utf-8"))
        notes = read_runtime_page("17.3", "u", "local", sections)
        self.assertEqual(notes.spark, "4.0.0")
        self.assertEqual(notes.environment["python"], "3.12.3")
        self.assertEqual(notes.environment["scala"], "2.13.16")
        self.assertEqual(len(notes.changes), 3)
        self.assertIn("`input_file_name`", notes.changes[0].title)

    def test_spark_version_is_found_wherever_the_page_mentions_it(self):
        pages = {
            "intro": "<h1>Databricks Runtime 17.0</h1><p>These notes cover the release, powered by Apache Spark 4.0.0.</p>",
            "deeper heading level": "<h1>Databricks Runtime 17.0</h1><h3>Overview</h3><p>Built on Apache Spark 4.0.0 and Delta.</p>",
            "in a list": "<h1>Runtime 17.0</h1><ul><li>Apache Spark 4.0.0 is the engine</li></ul>",
            "only a short mention": "<h1>Runtime 17.0 ships Spark 4.0</h1><p>Nothing else.</p>",
        }
        for label, html in pages.items():
            notes = read_runtime_page("17.0", "u", "local", parse_html(html))
            self.assertTrue(notes.spark.startswith("4.0"), label)

    def test_spark_statement_far_down_the_page_is_found(self):
        filler = "".join(f"<h3>Item {i}</h3><p>Nothing about the engine here.</p>" for i in range(30))
        html = ("<h1>Runtime 16.2</h1><p>Intro without a version.</p>" + filler +
                "<h2>Apache Spark</h2><p>Databricks Runtime 16.2 includes Apache Spark 3.5.2.</p>"
                "<h3>Other</h3><p>Earlier releases used Spark 3.4.</p>")
        self.assertEqual(read_runtime_page("16.2", "u", "local", parse_html(html)).spark, "3.5.2")

    def test_a_stray_short_mention_far_down_is_not_used(self):
        filler = "".join(f"<h3>Item {i}</h3><p>Nothing here.</p>" for i in range(30))
        html = "<h1>Runtime 16.4</h1><p>Intro.</p>" + filler + "<h3>Note</h3><p>See Spark 3.5 docs.</p>"
        self.assertEqual(read_runtime_page("16.4", "u", "local", parse_html(html)).spark, "")

    def test_a_runtime_with_unknown_spark_does_not_hide_the_spark_guide_rules(self):
        from ptagent.agents.release_notes import UpgradeBrief
        from ptagent.tools.release_notes import VersionNotes
        from ptagent.tools.rule_synthesis import synthesize
        brief = UpgradeBrief("16.4", "17.1")
        brief.versions = [VersionNotes("16.4", "u", "x", spark="3.5.2"), VersionNotes("17.0", "u", "x", spark=""),
                          VersionNotes("17.1", "u", "x", spark="4.0.0")]
        brief.guide_changes = [Change("spark-migration-guide", "4.0", "removed", "In Spark 4.0, `DataFrame.to_koalas` has been removed.", "u")]
        rules, _ = synthesize(brief, None)
        self.assertEqual(rules[0]["applies"]["introduced_in"], "17.0")  # earliest runtime that could have Spark 4.0

    def test_a_mention_of_an_older_spark_does_not_win_over_the_real_one(self):
        html = "<h1>Runtime</h1><p>Compared with Spark 3.5 the engine changed. It is powered by Apache Spark 4.0.0.</p>"
        self.assertEqual(read_runtime_page("17.0", "u", "local", parse_html(html)).spark, "4.0.0")

    def test_breaking_changes_are_flagged(self):
        sections = parse_html((FIXTURES / "16.0.html").read_text(encoding="utf-8"))
        notes = read_runtime_page("16.0", "u", "local", sections)
        self.assertTrue(notes.changes[0].breaking)

    def test_markdown_pages_work_too(self):
        sections = parse_markdown((FIXTURES / "17.0.md").read_text(encoding="utf-8"))
        notes = read_runtime_page("17.0", "u", "local", sections)
        self.assertEqual(notes.environment["java"], "Zulu17.58+21-CA")
        self.assertEqual([c.title for c in notes.changes], ["ANSI SQL enabled by default"])

    def test_spark_guide_items_are_limited_to_the_spark_range(self):
        sections = parse_html((FIXTURES / "pyspark_upgrade.html").read_text(encoding="utf-8"))
        items = read_spark_guide(sections, "u", (3, 5), (4, 0))
        self.assertEqual(len(items), 2)
        self.assertTrue(all(i.version == "4.0" for i in items))


class FetcherTests(unittest.TestCase):
    def test_probe_finds_versions_without_a_list_and_tries_the_target_after_a_gap(self):
        pages = {"https://x/16.0": "<h1>16.0</h1>", "https://x/16.1": "<h1>16.1</h1>", "https://x/17.0lts": "<h1>17.0</h1>",
                 "https://x/17.3lts": "<h1>17.3</h1>"}
        calls = []

        def opener(url, timeout):
            calls.append(url)
            return (200, pages[url]) if url in pages else (404, "")

        fetcher = Fetcher(tempfile.mkdtemp(), opener=opener)
        found, warnings = probe_runtime_pages(fetcher, "https://x/", (16, 0), (17, 3))
        self.assertEqual([v for v, *_ in found], [(16, 0), (16, 1), (17, 0), (17, 3)])
        self.assertEqual(warnings, [])

    def test_cache_is_used_and_offline_mode_never_touches_the_network(self):
        cache = tempfile.mkdtemp()
        calls = []

        def opener(url, timeout):
            calls.append(url)
            return 200, "<h1>page</h1>"

        Fetcher(cache, opener=opener).get("https://x/a")
        Fetcher(cache, opener=opener).get("https://x/a")
        self.assertEqual(len(calls), 1)
        text, origin = Fetcher(cache, offline=True, opener=opener).get("https://x/a")
        self.assertEqual(origin, "cache")
        text, origin = Fetcher(tempfile.mkdtemp(), offline=True, opener=opener).get("https://x/never")
        self.assertEqual((text, origin), (None, "missing"))
        self.assertEqual(len(calls), 1)

    def test_network_errors_become_warnings_not_crashes(self):
        def opener(url, timeout):
            raise ReleaseNotesError("cannot reach " + url)

        fetcher = Fetcher(tempfile.mkdtemp(), opener=opener)
        found, warnings = probe_runtime_pages(fetcher, "https://x/", (16, 0), (16, 1))
        self.assertEqual(found, [])
        self.assertTrue(warnings)


class BriefAndRulesTests(unittest.TestCase):
    def setUp(self):
        self.cfg, self.agent = offline_agent(pyspark_guide_url="https://spark/pyspark_upgrade",
                                             sql_guide_url="https://spark/sql_migration_guide")
        self.brief = self.agent.build_brief("15.4", "17.3")
        self.rules, self.advisories, self.review = self.agent.rules(self.brief)
        self.by_id = {r["id"]: r for r in self.rules}

    def test_brief_lists_the_versions_and_the_environment_changes(self):
        self.assertEqual([v.version for v in self.brief.versions], ["15.4", "16.0", "17.0", "17.3"])
        diff = self.brief.env_diff()
        self.assertEqual(diff["python"], {"from": "3.11.0", "to": "3.12.3"})
        self.assertEqual(diff["scala"], {"from": "2.12.15", "to": "2.13.16"})
        self.assertEqual(diff["spark"], {"from": "3.5.0", "to": "4.0.0"})

    def test_python_rules_come_from_the_python_versions_the_upgrade_crosses(self):
        rule = self.by_id["PY3.12"]
        self.assertEqual(rule["applies"]["introduced_in"], "16.0")
        names = {n for m in rule["matchers"] for n in m.get("names", [])}
        self.assertIn("distutils", names)
        self.assertNotIn("PY3.13", self.by_id)  # Python 3.13 is not reached
        self.assertNotIn("PY3.11", self.by_id)  # already on 3.11

    def test_scala_rule_uses_the_versions_from_the_notes(self):
        rule = self.by_id["ENV-scala"]
        self.assertEqual(rule["matchers"][0]["substrings"], ["_2.12"])
        self.assertEqual(rule["applies"]["introduced_in"], "17.0")

    def test_behavioral_changes_become_rules_and_the_baseline_runtime_is_ignored(self):
        titles = " ".join(r["title"] for r in self.rules)
        self.assertIn("input_file_name", titles)
        self.assertNotIn("old_baseline_function", json.dumps(self.rules))
        auto_loader = next(r for r in self.rules if "Auto Loader" in r["title"])
        self.assertEqual(auto_loader["matchers"], [{"type": "string_contains", "substrings": ["cloudFiles.useIncrementalListing"]}])

    def test_replacement_names_are_not_treated_as_the_problem(self):
        rule = next(r for r in self.rules if "to_koalas" in r["title"])
        names = [n for m in rule["matchers"] for n in m.get("names", [])]
        self.assertEqual(names, [".to_koalas"])  # pandas_api is the replacement

    def test_generic_names_are_not_matched(self):
        change = Change("spark-migration-guide", "4.0", "append removed", "In Spark 4.0, `DataFrame.append` has been removed. Use `ps.concat` instead.", "u")
        self.assertIsNone(rule_from_change(change, "X", "17.0"))

    def test_option_keys_without_the_spark_prefix_are_matched_as_text(self):
        change = Change("databricks-release-notes", "16.3", "Source option behavior changed",
                        "The default of `kafka.sasl.client.callback.handler.class` changed. Set it explicitly.", "u")
        rule = rule_from_change(change, "X", "16.3")
        self.assertEqual(rule["matchers"], [{"type": "string_contains", "substrings": ["kafka.sasl.client.callback.handler.class"]}])

    def test_improvements_and_fixes_are_advisories_unless_marked_breaking(self):
        text = "The `some_function` function now fails with a clearer message, and the old behavior is no longer supported."
        change = Change("databricks-release-notes", "16.3", "Improved error message for something", text, "u")
        self.assertIsNone(rule_from_change(change, "X", "16.3"))
        breaking = Change("databricks-release-notes", "16.3", "Improved error message for something", text, "u", breaking=True)
        self.assertIsNotNone(rule_from_change(breaking, "X", "16.3"))

    def test_error_types_and_scala_constants_are_never_matched(self):
        change = Change("spark-migration-guide", "4.0", "Error changed",
                        "Since Spark 4.0, the default of `spark.sql.maxSinglePartitionBytes` changed; it is no longer `Long.MaxValue`, and `AnalysisException` is thrown.", "u")
        rule = rule_from_change(change, "X", "17.0")
        names = [n for m in rule["matchers"] for n in m.get("substrings", []) + m.get("names", [])]
        self.assertEqual(names, ["spark.sql.maxSinglePartitionBytes"])

    def test_a_named_option_is_the_subject_and_functions_mentioned_with_it_are_context(self):
        change = Change("spark-migration-guide", "4.0", "Option changed",
                        "Since Spark 4.0, the SQL config `spark.sql.legacy.allowZeroIndexInFormatString` is removed, which affects `format_string`.", "u")
        rule = rule_from_change(change, "X", "17.0")
        self.assertEqual(rule["matchers"], [{"type": "string_contains", "substrings": ["spark.sql.legacy.allowZeroIndexInFormatString"]}])

    def test_more_wordings_count_as_a_breaking_change(self):
        for wording in ("is no longer allowed", "is not allowed", "is now blocked", "now requires explicit casts", "is end-of-life"):
            change = Change("databricks-release-notes", "16.0", "Behavior", f"The `some_old_function` function {wording}.", "u")
            self.assertIsNotNone(rule_from_change(change, "X", "16.0"), wording)

    def test_python_api_paths_are_matched_as_attributes_not_as_text(self):
        change = Change("spark-migration-guide", "4.0", "removed", "In Spark 4.0, `pyspark.pandas.read_excel_legacy` has been removed.", "u")
        rule = rule_from_change(change, "X", "17.0")
        self.assertEqual(rule["matchers"], [{"type": "dotted_suffix", "names": [".read_excel_legacy"]}])

    def test_notes_without_code_patterns_are_kept_as_advisories(self):
        self.assertTrue(any("case class" in a["title"] for a in self.advisories))

    def test_an_option_that_becomes_the_default_activates_the_ansi_plugin(self):
        ansi = [r for r in self.rules if r.get("emulation", {}).get("conf") == "spark.sql.ansi.enabled"]
        self.assertTrue(ansi)
        extra = plugin_rules(self.rules)
        self.assertEqual([r["id"] for r in extra], ["P-ANSI"])
        self.assertEqual(extra[0]["plugin"], "ansi")
        target, current = emulation_confs([{"emulation": ansi[0]["emulation"]}])
        self.assertEqual(target, {"spark.sql.ansi.enabled": "true"})
        self.assertEqual(current, {"spark.sql.ansi.enabled": "false"})

    def test_java_change_is_reported_for_manual_review(self):
        self.assertEqual(self.review[0]["rule"], "ENV-java")

    def test_generated_rules_match_code(self):
        def hit(rule_id, source):
            rule = self.by_id[rule_id]
            tree = ast.parse(source)
            return any(matches(n, m) for n in ast.walk(tree) for m in rule["matchers"])

        self.assertTrue(hit("PY3.12", "import distutils.version"))
        self.assertTrue(hit("PY3.12", "self.assertEquals(1, 1)"))
        self.assertFalse(hit("PY3.12", "import os"))
        self.assertTrue(hit("D17.3-3", "df.select(F.input_file_name())"))

    def test_emulation_environment_variables(self):
        env = emulation_env({"spark.sql.ansi.enabled": "true"})
        self.assertEqual(env["PYSPARK_SUBMIT_ARGS"], "--conf spark.sql.ansi.enabled=true pyspark-shell")
        self.assertEqual(json.loads(env["PTAGENT_SPARK_CONF"]), {"spark.sql.ansi.enabled": "true"})
        self.assertEqual(env["PTAGENT_ANSI"], "true")


class EndToEndTests(unittest.TestCase):
    def run_mode_c(self, project, **notes):
        cfg = load_config()
        cfg.run_tests = "never"
        cfg.pyspark_guide_url = "https://spark/pyspark_upgrade"
        cfg.sql_guide_url = "https://spark/sql_migration_guide"
        options = {"rules": "dynamic", "local_dir": str(FIXTURES), "offline": True, **notes}
        with tempfile.TemporaryDirectory() as tmp:
            report = Orchestrator(cfg).run(SAMPLES / project, mode="C", from_dbr="15.4", to_dbr="17.3",
                                           out_root=Path(tmp), notes=options)
            files = sorted(p.name for p in (Path(tmp) / project).iterdir())
        return report, files

    def test_dynamic_mode_finds_the_impact_and_writes_the_brief(self):
        report, files = self.run_mode_c("legacy_runtime")
        ids = {f["rule_id"] for f in report["runtime_impact"]["findings"]}
        self.assertIn("PY3.12", ids)
        self.assertIn("upgrade_brief.json", files)
        self.assertIn("generated_rules.json", files)
        self.assertEqual(report["upgrade_brief"]["mode"], "dynamic")
        self.assertEqual(len(report["upgrade_brief"]["versions"]), 4)

    def test_ansi_functions_get_migrated_tests_because_the_notes_say_ansi_is_on(self):
        report, _ = self.run_mode_c("orders_ansi_demo")
        migration = report["migration"]
        self.assertEqual(migration["migrated_functions"], ["orders_etl.cleaning.clean_orders"])
        self.assertEqual(migration["emulated_options"], {"spark.sql.ansi.enabled": "true"})

    def test_nothing_in_the_notes_means_nothing_is_emulated(self):
        cfg = load_config()
        cfg.run_tests = "never"
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "notes"
            empty.mkdir()
            (empty / "16.0.html").write_text((FIXTURES / "16.0.html").read_text(encoding="utf-8"), encoding="utf-8")
            report = Orchestrator(cfg).run(SAMPLES / "orders_ansi_demo", mode="C", from_dbr="16.0", to_dbr="16.0",
                                           out_root=Path(tmp) / "out", notes={"rules": "dynamic", "local_dir": str(empty), "offline": True})
        self.assertEqual(report["migration"]["emulated_options"], {})
        self.assertEqual(report["migration"]["migrated_functions"], [])

    def test_falls_back_to_the_curated_rules_when_nothing_can_be_read(self):
        cfg = load_config()
        cfg.run_tests = "never"
        with tempfile.TemporaryDirectory() as tmp:
            report = Orchestrator(cfg).run(SAMPLES / "legacy_runtime", mode="C", from_dbr="15.4", to_dbr="17.3",
                                           out_root=Path(tmp) / "out",
                                           notes={"rules": "dynamic", "offline": True, "local_dir": str(Path(tmp) / "none")})
        self.assertTrue(any("curated" in w for w in report["upgrade_brief"]["warnings"]))
        self.assertTrue(report["runtime_impact"]["findings"])


if __name__ == "__main__":
    unittest.main()
