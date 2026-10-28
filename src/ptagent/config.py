"""Configuration loading."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "config.yaml"


@dataclass
class LLMConfig:
    backend: str = "offline"
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:7b"
    timeout_s: int = 120
    temperature: float = 0.1
    max_tokens: int = 600
    keep_alive: str = "10m"


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    output_dir: Path = REPO_ROOT / "outputs"
    dbr_rules_path: Path = REPO_ROOT / "knowledge" / "dbr_rules.json"
    guidelines_path: Path = REPO_ROOT / "knowledge" / "testing_guidelines.md"
    prompts_dir: Path = REPO_ROOT / "prompts"
    dataframe_param_names: list = field(default_factory=lambda: ["df", "input_df", "sdf", "data"])
    entry_point_names: list = field(default_factory=lambda: ["main"])
    skip_dirs: list = field(default_factory=lambda: [".git", "__pycache__", ".venv", "venv", "outputs"])
    run_tests: str = "auto"
    max_repair_attempts: int = 2
    coverage_target: float = 80.0
    verify_timeout_s: int = 900
    from_dbr: str = "13.3"
    to_dbr: str = "17.3"


def _resolve(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPO_ROOT / path


def load_config(path: Optional[Path] = None) -> Config:
    """Load configs/config.yaml (or a custom file) into a Config object."""
    path = Path(path) if path else DEFAULT_CONFIG
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw = raw or {}
    llm_raw = raw.get("llm", {}) or {}
    paths = raw.get("paths", {}) or {}
    analysis = raw.get("analysis", {}) or {}
    verification = raw.get("verification", {}) or {}
    upgrade = raw.get("runtime_upgrade", {}) or {}
    cfg = Config(llm=LLMConfig(**{k: v for k, v in llm_raw.items() if k in LLMConfig.__dataclass_fields__}))
    if "output_dir" in paths:
        cfg.output_dir = _resolve(paths["output_dir"])
    if "dbr_rules" in paths:
        cfg.dbr_rules_path = _resolve(paths["dbr_rules"])
    if "guidelines" in paths:
        cfg.guidelines_path = _resolve(paths["guidelines"])
    if "prompts_dir" in paths:
        cfg.prompts_dir = _resolve(paths["prompts_dir"])
    cfg.dataframe_param_names = analysis.get("dataframe_param_names", cfg.dataframe_param_names)
    cfg.entry_point_names = analysis.get("entry_point_names", cfg.entry_point_names)
    cfg.skip_dirs = analysis.get("skip_dirs", cfg.skip_dirs)
    cfg.run_tests = verification.get("run_tests", cfg.run_tests)
    cfg.max_repair_attempts = int(verification.get("max_repair_attempts", cfg.max_repair_attempts))
    cfg.coverage_target = float(verification.get("coverage_target", cfg.coverage_target))
    cfg.verify_timeout_s = int(verification.get("timeout_s", cfg.verify_timeout_s))
    cfg.from_dbr = str(upgrade.get("from_dbr", cfg.from_dbr))
    cfg.to_dbr = str(upgrade.get("to_dbr", cfg.to_dbr))
    return cfg
