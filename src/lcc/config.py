"""Harness configuration: `lcc.config.toml` plus environment overrides. Secrets come only from the environment."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
CONFIG_ENV = "LCC_CONFIG"
KEY_ENV = "AI_API_KEY"


@dataclass
class ModelConfig:
    provider: str = "auto"
    model: str = ""
    base_url: str = ""
    temperature: float = 0.0
    seed: int | None = 7
    max_output_tokens: int = 4096
    model_fast: str = ""
    model_strong: str = ""
    defaults: dict[str, str] = field(default_factory=dict)


@dataclass
class RunConfig:
    max_iterations: int = 5
    token_budget: int = 300_000
    coder_max_steps: int = 30
    workspaces_dir: str = "workspaces"
    outputs_dir: str = "outputs"
    prepare_env: bool = True


@dataclass
class Config:
    model: ModelConfig
    run: RunConfig
    path: Path | None = None

    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else ((self.path.parent if self.path else ROOT) / p)


def _pick(cls: type, raw: dict[str, Any]) -> Any:
    known = cls.__dataclass_fields__
    return cls(**{k: v for k, v in raw.items() if k in known})


def load_config(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get(CONFIG_ENV) or ROOT / "lcc.config.toml")
    raw: dict[str, Any] = {}
    if path.is_file():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    m = dict(raw.get("model") or {})
    m.setdefault("defaults", {})
    model = _pick(ModelConfig, m)
    if model.seed is not None and model.seed < 0:
        model.seed = None
    env = os.environ
    model.provider = (env.get("LCC_PROVIDER") or model.provider or "auto").lower()
    model.model = env.get("LCC_MODEL") or model.model
    model.base_url = env.get("LCC_BASE_URL") or model.base_url
    model.model_fast = env.get("LCC_MODEL_FAST") or model.model_fast
    model.model_strong = env.get("LCC_MODEL_STRONG") or model.model_strong
    return Config(model=model, run=_pick(RunConfig, raw.get("run") or {}), path=path if path.is_file() else None)


def api_key(provider_key_env: str = "") -> str:
    """AI_API_KEY is the evaluation credential. Legacy per-provider variables are a local-dev fallback."""
    for name in (KEY_ENV, "LCC_API_KEY", provider_key_env):
        if name and os.environ.get(name):
            return os.environ[name].strip()
    return ""
