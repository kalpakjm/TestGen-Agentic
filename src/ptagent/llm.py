"""LLM backends. The default 'offline' backend needs no model: it returns the deterministic template.

The 'ollama' backend sends the prompt to a locally running open-weight model (free, no API key).
Every LLM answer is validated by the calling agent and replaced by the template if it is unusable.
"""
from __future__ import annotations

import json
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

    def generate(self, request: GenerationRequest) -> GenerationResult:
        return GenerationResult(request.fallback(), used_llm=False, note="deterministic template")


class OllamaBackend:
    name = "ollama"

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
    return OfflineBackend()
