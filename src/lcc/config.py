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
    # Seconds for one full test-suite run; a timed-out full run falls back to the targeted tests.
    test_timeout: int = 900
    # Send one tiny completion before starting work (fails fast on a bad key, no balance, unknown model).
    preflight: bool = True
    # "none": target code runs on this machine with credentials stripped and git guarded.
    # "docker": target code (install, tests, shell) runs in a container with no network and no credentials.
    sandbox: str = "auto"
    # Ablation: model calls to skip, to measure whether they earn their tokens (env LCC_ABLATE="planner,reviewer").
    ablate: list = field(default_factory=list)
    # Test-strength check after a proven fix: "off", "report" (record the score), or "gate" (a test that catches
    # none of 3+ deliberate breaks of the fix sends the coder back once to strengthen it). Env LCC_MUTATION.
    mutation: str = "report"
    mutation_limit: int = 6
    # An optimization is accepted only if its benchmark (.lcc/bench.py) is at least this much faster.
    min_speedup: float = 1.1


@dataclass
class AutoConfig:
    """Repo-only mode: find problems without an issue, fix them, open pull requests."""
    max_fixes: int = 3
    audit_calls: int = 2  # model calls when audit_scope = "top"
    audit_chars: int = 18_000  # source characters per audit call (~4.5k tokens)
    # "full": the model reads the whole repository, most central files first, up to audit_budget_chars
    # (120k chars ~ 30k tokens ~ 7 calls); the report states exactly how much was read. "top": audit_calls chunks.
    audit_scope: str = "full"
    audit_budget_chars: int = 120_000
    open_pr: bool = True
    pr_draft: bool = True  # maintainers see a draft first; mark it ready yourself
    max_open_prs: int = 3  # never have more than this many open harness PRs on one repository


@dataclass
class Config:
    model: ModelConfig
    run: RunConfig
    path: Path | None = None
    auto: AutoConfig = field(default_factory=AutoConfig)

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
    run = _pick(RunConfig, raw.get("run") or {})
    run.sandbox = (env.get("LCC_SANDBOX") or run.sandbox or "auto").lower()
    run.mutation = (env.get("LCC_MUTATION") or run.mutation or "report").lower()
    if env.get("LCC_ABLATE") is not None:
        run.ablate = [a.strip().lower() for a in env["LCC_ABLATE"].split(",") if a.strip()]
    auto = _pick(AutoConfig, raw.get("auto") or {})
    if env.get("LCC_OPEN_PR"):
        auto.open_pr = env["LCC_OPEN_PR"].strip().lower() not in {"0", "false", "no", "off"}
    return Config(model=model, run=run, path=path if path.is_file() else None, auto=auto)


def api_key() -> str:
    """AI_API_KEY is the only credential the harness reads."""
    return (os.environ.get(KEY_ENV) or "").strip()
