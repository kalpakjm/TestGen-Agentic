import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from ptagent.config import LLMConfig
from ptagent.llm import (AnthropicBackend, GeminiBackend, GenerationRequest, OfflineBackend, OllamaBackend,
                         OpenAIBackend, build_backend)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def reply(body: dict):
    return FakeResponse(json.dumps(body).encode("utf-8"))


REQUEST = GenerationRequest("test_generation", "write a test", lambda: "FALLBACK")
CODE = "```python\ndef test_x():\n    assert 1\n```"


class HostedBackendTests(unittest.TestCase):
    def run_backend(self, backend, body, env):
        captured = {}

        def fake_urlopen(http_request, timeout=0):
            captured["url"] = http_request.full_url
            captured["headers"] = {k.lower(): v for k, v in http_request.header_items()}
            captured["payload"] = json.loads(http_request.data.decode("utf-8"))
            return reply(body)

        with mock.patch.dict(os.environ, env, clear=True), mock.patch("urllib.request.urlopen", fake_urlopen):
            return backend.generate(REQUEST), captured

    def test_openai_request_and_answer(self):
        result, sent = self.run_backend(OpenAIBackend("m1"), {"choices": [{"message": {"content": CODE}}]}, {"OPENAI_API_KEY": "k"})
        self.assertTrue(result.used_llm)
        self.assertIn("def test_x", result.text)
        self.assertTrue(sent["url"].endswith("/chat/completions"))
        self.assertEqual(sent["headers"]["authorization"], "Bearer k")
        self.assertEqual(sent["payload"]["messages"][0]["content"], "write a test")

    def test_openai_compatible_server_url(self):
        _, sent = self.run_backend(OpenAIBackend("m1", base_url="http://localhost:1234/v1/"),
                                   {"choices": [{"message": {"content": CODE}}]}, {"OPENAI_API_KEY": "k"})
        self.assertEqual(sent["url"], "http://localhost:1234/v1/chat/completions")

    def test_gemini_request_and_answer(self):
        body = {"candidates": [{"content": {"parts": [{"text": CODE}]}}]}
        result, sent = self.run_backend(GeminiBackend("gemini-x"), body, {"GOOGLE_API_KEY": "g"})
        self.assertTrue(result.used_llm)
        self.assertIn("models/gemini-x:generateContent", sent["url"])
        self.assertEqual(sent["headers"]["x-goog-api-key"], "g")
        self.assertNotIn("g", sent["url"].split("?")[-1] if "?" in sent["url"] else "")  # key is never put in the URL

    def test_anthropic_request_and_answer(self):
        body = {"content": [{"type": "text", "text": CODE}]}
        result, sent = self.run_backend(AnthropicBackend("claude-x", max_tokens=1000), body, {"ANTHROPIC_API_KEY": "a"})
        self.assertTrue(result.used_llm)
        self.assertEqual(sent["headers"]["x-api-key"], "a")
        self.assertEqual(sent["payload"]["max_tokens"], 1000)

    def test_missing_key_falls_back_to_the_template_without_a_request(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("urllib.request.urlopen") as opened:
            result = OpenAIBackend("m1").generate(REQUEST)
        opened.assert_not_called()
        self.assertEqual((result.text, result.used_llm), ("FALLBACK", False))
        self.assertIn("OPENAI_API_KEY", result.note)

    def test_http_errors_and_odd_answers_fall_back(self):
        error = urllib.error.HTTPError("u", 429, "rate", {}, io.BytesIO(b'{"error": "slow down"}'))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "a"}, clear=True), \
                mock.patch("urllib.request.urlopen", side_effect=error):
            result = AnthropicBackend("m").generate(REQUEST)
        self.assertEqual((result.text, result.used_llm), ("FALLBACK", False))
        self.assertIn("429", result.note)
        result, _ = self.run_backend(GeminiBackend("m"), {"candidates": []}, {"GEMINI_API_KEY": "g"})
        self.assertEqual((result.text, result.used_llm), ("FALLBACK", False))

    def test_build_backend_picks_the_provider(self):
        for name, kind in (("openai", OpenAIBackend), ("gemini", GeminiBackend), ("anthropic", AnthropicBackend),
                           ("ollama", OllamaBackend), ("offline", OfflineBackend)):
            cfg = LLMConfig(backend=name)
            self.assertIsInstance(build_backend(cfg), kind)
        self.assertGreaterEqual(build_backend(LLMConfig(backend="openai")).max_tokens, 2048)
        with self.assertRaises(ValueError):
            build_backend(LLMConfig(backend="nope"))

    def test_hosted_and_ollama_backends_count_as_llm_backends(self):
        self.assertTrue(all(b.uses_llm for b in (OpenAIBackend, GeminiBackend, AnthropicBackend, OllamaBackend)))
        self.assertFalse(OfflineBackend.uses_llm)


if __name__ == "__main__":
    unittest.main()
