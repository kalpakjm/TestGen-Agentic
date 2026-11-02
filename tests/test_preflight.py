import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import SAMPLES

from ptagent.cli import main
from ptagent.config import LLMConfig
from ptagent.preflight import UsageError, check_backend, check_project, check_versions


class ProjectAndVersionChecks(unittest.TestCase):
    def test_missing_folder_file_and_empty_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(UsageError, "not found"):
                check_project(Path(tmp) / "nope")
            file = Path(tmp) / "a.py"
            file.write_text("x = 1\n")
            with self.assertRaisesRegex(UsageError, "not a folder"):
                check_project(file)
            empty = Path(tmp) / "empty"
            empty.mkdir()
            with self.assertRaisesRegex(UsageError, "no .py files"):
                check_project(empty)
        self.assertEqual(check_project(SAMPLES / "orders_no_tests"), SAMPLES / "orders_no_tests")

    def test_versions(self):
        check_versions(None, None)
        check_versions("15.4", "17.3 LTS")
        for args, message in ((("15.4", None), "both"), (("15", "17.3"), "not a Databricks"),
                              (("17.3", "15.4"), "newer")):
            with self.assertRaisesRegex(UsageError, message):
                check_versions(*args)


class BackendChecks(unittest.TestCase):
    def test_unreachable_ollama_falls_back_to_offline_with_one_warning(self):
        llm = LLMConfig(backend="ollama", ollama_model="qwen2.5-coder:3b")
        warnings = check_backend(llm, models_lookup=lambda url: None)
        self.assertEqual(llm.backend, "offline")
        self.assertIn("not reachable", warnings[0])

    def test_missing_model_names_the_pull_command(self):
        llm = LLMConfig(backend="ollama", ollama_model="qwen2.5-coder:3b")
        warnings = check_backend(llm, models_lookup=lambda url: {"llama3:latest"})
        self.assertEqual(llm.backend, "offline")
        self.assertIn("ollama pull qwen2.5-coder:3b", warnings[0])

    def test_installed_model_is_kept(self):
        llm = LLMConfig(backend="ollama", ollama_model="qwen2.5-coder:3b")
        self.assertEqual(check_backend(llm, models_lookup=lambda url: {"qwen2.5-coder:3b"}), [])
        self.assertEqual(llm.backend, "ollama")

    def test_hosted_backend_without_a_key(self):
        llm = LLMConfig(backend="gemini")
        with mock.patch.dict(os.environ, {}, clear=True):
            warnings = check_backend(llm)
        self.assertEqual(llm.backend, "offline")
        self.assertIn("GEMINI_API_KEY", warnings[0])


class CommandLineErrors(unittest.TestCase):
    def test_usage_errors_exit_with_code_2_and_no_traceback(self):
        with mock.patch("sys.stderr") as err:
            self.assertEqual(main(["analyze", "does/not/exist"]), 2)
            self.assertEqual(main(["run", str(SAMPLES / "orders_no_tests"), "--from-dbr", "15.4"]), 2)
        written = "".join(call.args[0] for call in err.write.call_args_list)
        self.assertIn("project folder not found", written)
        self.assertNotIn("Traceback", written)

    def test_unexpected_errors_give_one_line_and_exit_code_1(self):
        with mock.patch("ptagent.cli.build_index", side_effect=RuntimeError("boom")), mock.patch("sys.stderr") as err:
            self.assertEqual(main(["analyze", str(SAMPLES / "orders_no_tests")]), 1)
        written = "".join(call.args[0] for call in err.write.call_args_list)
        self.assertIn("RuntimeError: boom", written)
        with mock.patch("ptagent.cli.build_index", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                main(["--debug", "analyze", str(SAMPLES / "orders_no_tests")])


if __name__ == "__main__":
    unittest.main()
