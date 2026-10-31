"""LLM backends. The default 'offline' backend needs no model: it returns the deterministic template.

  * 'ollama'    a locally running open-weight model (free, no API key, code stays on your machine);
  * 'openai'    OpenAI Chat Completions, or any OpenAI-compatible server through `openai_base_url`;
  * 'gemini'    Google Gemini generateContent;
  * 'anthropic' Anthropic Messages (Claude).
The three hosted backends send the prompts (which contain your function source code) to that provider and
need an API key in an environment variable (OPENAI_API_KEY, GEMINI_API_KEY, ANTHROPIC_API_KEY); keys are
never read from the config file. They use only the standard library, so no extra package is needed.
Every LLM answer is validated by the calling agent and replaced by the template if it is unusable.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable


@dataclass
class GenerationRequest:
    task: str
    prompt: str
    fallback: Callable[[], str]


@dataclass
class GenerationResult:
    text: str
    used_llm: bool
    note: str = ""


class OfflineBackend:
    name = "offline-template"
    uses_llm = False

    def generate(self, request: GenerationRequest) -> GenerationResult:
        return GenerationResult(request.fallback(), used_llm=False, note="deterministic template")


class OllamaBackend:
    name = "ollama"
    uses_llm = True

    def __init__(self, url: str, model: str, timeout_s: int = 120, temperature: float = 0.1, max_tokens: int = 600,
                 keep_alive: str = "10m"):
        self.max_tokens = max_tokens
        self.keep_alive = keep_alive
        self.url = url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.temperature = temperature

    def generate(self, request: GenerationRequest) -> GenerationResult:
        payload = json.dumps({
            "model": self.model,
            "prompt": request.prompt,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": self.temperature, "num_predict": self.max_tokens},
        }).encode("utf-8")
        http_request = urllib.request.Request(
            f"{self.url}/api/generate", data=payload, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_s) as response:
                body = json.loads(response.read().decode("utf-8"))
            text = extract_code(body.get("response", ""))
            if not text.strip():
                raise ValueError("empty response")
            return GenerationResult(text, used_llm=True, note=f"model {self.model}")
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            return GenerationResult(request.fallback(), used_llm=False, note=f"ollama unavailable ({exc}); used template")


def _unload(self):
    """Ask Ollama to free the model's memory now (keep_alive 0). Errors are ignored."""
    payload = json.dumps({"model": self.model, "keep_alive": 0}).encode("utf-8")
    request = urllib.request.Request(f"{self.url}/api/generate", data=payload,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30):
            pass
    except (urllib.error.URLError, OSError):
        pass


OllamaBackend.unload = _unload


class HostedBackend:
    """Base class for API providers. Subclasses define the request and how to read the answer."""
    name = "hosted"
    uses_llm = True
    key_env: tuple = ()
    remote = True

    def __init__(self, model: str, timeout_s: int = 120, temperature: float = 0.1, max_tokens: int = 600,
                 base_url: str = ""):
        self.model = model
        self.timeout_s = timeout_s
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.base_url = base_url.rstrip("/")

    def api_key(self) -> str:
        for var in self.key_env:
            if os.environ.get(var):
                return os.environ[var]
        return ""

    def build(self, prompt: str, key: str):
        """Return (url, headers, payload)."""
        raise NotImplementedError

    def parse(self, body: dict) -> str:
        raise NotImplementedError

    def generate(self, request: GenerationRequest) -> GenerationResult:
        key = self.api_key()
        if not key:
            return GenerationResult(request.fallback(), used_llm=False,
                                    note=f"{self.name}: set {' or '.join(self.key_env)}; used template")
        url, headers, payload = self.build(request.prompt, key)
        http_request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                              headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(http_request, timeout=self.timeout_s) as response:
                body = json.loads(response.read().decode("utf-8"))
            text = extract_code(self.parse(body))
            if not text.strip():
                raise ValueError("empty response")
            return GenerationResult(text, used_llm=True, note=f"model {self.model}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200] if hasattr(exc, "read") else ""
            return GenerationResult(request.fallback(), used_llm=False,
                                    note=f"{self.name} error {exc.code}: {detail}; used template")
        except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            return GenerationResult(request.fallback(), used_llm=False, note=f"{self.name} unavailable ({exc}); used template")


class OpenAIBackend(HostedBackend):
    name = "openai"
    key_env = ("OPENAI_API_KEY",)

    def build(self, prompt, key):
        base = self.base_url or "https://api.openai.com/v1"
        return (f"{base}/chat/completions", {"Authorization": f"Bearer {key}"},
                {"model": self.model, "messages": [{"role": "user", "content": prompt}],
                 "temperature": self.temperature, "max_completion_tokens": self.max_tokens})

    def parse(self, body):
        return body["choices"][0]["message"]["content"] or ""


class GeminiBackend(HostedBackend):
    name = "gemini"
    key_env = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def build(self, prompt, key):
        base = self.base_url or "https://generativelanguage.googleapis.com/v1beta"
        return (f"{base}/models/{self.model}:generateContent", {"x-goog-api-key": key},
                {"contents": [{"parts": [{"text": prompt}]}],
                 "generationConfig": {"temperature": self.temperature, "maxOutputTokens": self.max_tokens}})

    def parse(self, body):
        return "".join(part.get("text", "") for part in body["candidates"][0]["content"]["parts"])


class AnthropicBackend(HostedBackend):
    name = "anthropic"
    key_env = ("ANTHROPIC_API_KEY",)

    def build(self, prompt, key):
        base = self.base_url or "https://api.anthropic.com/v1"
        return (f"{base}/messages", {"x-api-key": key, "anthropic-version": "2023-06-01"},
                {"model": self.model, "max_tokens": self.max_tokens, "temperature": self.temperature,
                 "messages": [{"role": "user", "content": prompt}]})

    def parse(self, body):
        return "".join(block.get("text", "") for block in body["content"] if block.get("type") == "text")


BACKEND_NAMES = ["offline", "ollama", "openai", "gemini", "anthropic"]


def extract_code(text: str) -> str:
    """Return the code from a model answer.

    Handles a closed fenced block, an unclosed one (answer cut off by the token limit) and plain code.
    """
    match = re.search(r"```(?:python)?[ \t]*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip("\n")
    opening = re.match(r"\s*```(?:python)?[ \t]*\n", text)
    if opening:
        return text[opening.end():].rstrip()
    return text.strip()


def build_backend(llm_config):
    if llm_config.backend == "ollama":
        return OllamaBackend(llm_config.ollama_url, llm_config.ollama_model,
                             llm_config.timeout_s, llm_config.temperature, llm_config.max_tokens,
                             llm_config.keep_alive)
    # hosted models are fast and some spend part of the limit on reasoning, so they get a roomier answer limit
    room = max(llm_config.max_tokens, 2048)
    if llm_config.backend == "openai":
        return OpenAIBackend(llm_config.openai_model, llm_config.timeout_s, llm_config.temperature, room,
                             llm_config.openai_base_url)
    if llm_config.backend == "gemini":
        return GeminiBackend(llm_config.gemini_model, llm_config.timeout_s, llm_config.temperature, room)
    if llm_config.backend == "anthropic":
        return AnthropicBackend(llm_config.anthropic_model, llm_config.timeout_s, llm_config.temperature, room)
    if llm_config.backend not in ("offline", "", None):
        raise ValueError(f"unknown llm backend {llm_config.backend!r}; choose from {', '.join(BACKEND_NAMES)}")
    return OfflineBackend()
