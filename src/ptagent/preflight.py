"""Checks run before any agent starts, so that common setup problems give one clear message
instead of a traceback or a long run that silently falls back."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path


class UsageError(Exception):
    """A problem with what the user asked for (wrong path, bad version). Exit code 2."""


def check_project(path) -> Path:
    project = Path(path)
    if not project.exists():
        raise UsageError(f"project folder not found: {project}")
    if not project.is_dir():
        raise UsageError(f"not a folder: {project} (give the project's root folder, not a file)")
    has_code = any(p.suffix in (".py", ".ipynb") for p in project.rglob("*")
                   if not any(part.startswith(".") or part in ("venv", "__pycache__") for part in p.parts))
    if not has_code:
        raise UsageError(f"no .py files or notebooks found under {project}")
    return project


VERSION = re.compile(r"^\d+\.\d+(\s*LTS)?$", re.I)


def check_versions(from_dbr, to_dbr):
    if bool(from_dbr) != bool(to_dbr):
        raise UsageError("give both --from-dbr and --to-dbr (for example --from-dbr 15.4 --to-dbr 17.3)")
    if not from_dbr:
        return
    for name, value in (("--from-dbr", from_dbr), ("--to-dbr", to_dbr)):
        if not VERSION.match(str(value).strip()):
            raise UsageError(f"{name} {value!r} is not a Databricks Runtime version such as 15.4 or 17.3")
    numbers = lambda v: tuple(int(n) for n in re.findall(r"\d+", v)[:2])
    if numbers(from_dbr) > numbers(to_dbr):
        raise UsageError(f"--from-dbr {from_dbr} is newer than --to-dbr {to_dbr}")


def _ollama_models(url: str, timeout: float = 5.0):
    """Names of the models Ollama has, or None when the server cannot be reached."""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/tags", timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return {m.get("name", "") for m in body.get("models", [])}


KEYS = {"openai": ("OPENAI_API_KEY",), "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "anthropic": ("ANTHROPIC_API_KEY",)}


def check_backend(llm, models_lookup=_ollama_models) -> list:
    """Return warnings. When the chosen model cannot be used, switch to the offline templates
    (the run still completes) and say why, once, before the run starts."""
    warnings = []
    if llm.backend == "ollama":
        models = models_lookup(llm.ollama_url)
        wanted = llm.ollama_model
        if models is None:
            warnings.append(f"Ollama is not reachable at {llm.ollama_url}. Start the Ollama app (or `ollama serve`) and run again. "
                            "Continuing with the offline templates.")
            llm.backend = "offline"
        elif wanted not in models and f"{wanted}:latest" not in models:
            have = ", ".join(sorted(models)) or "none"
            warnings.append(f"Ollama has no model {wanted!r} (installed: {have}). Run `ollama pull {wanted}`, "
                            "or set llm.ollama_model / --model. Continuing with the offline templates.")
            llm.backend = "offline"
    elif llm.backend in KEYS:
        names = KEYS[llm.backend]
        if not any(os.environ.get(n) for n in names):
            warnings.append(f"backend {llm.backend!r} needs the environment variable {' or '.join(names)}. "
                            "Continuing with the offline templates.")
            llm.backend = "offline"
    return warnings
