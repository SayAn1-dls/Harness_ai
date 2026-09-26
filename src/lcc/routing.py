"""Model routing: cheap model for classification-style agents, strong model for hard reasoning."""

from __future__ import annotations


from lcc.model import BaseProvider, ChatResult, OpenAICompatibleProvider
from lcc.schemas import Lane, TaskState

FAST_AGENTS = {"intake", "recovery", "reviewer", "context"}
STRONG_AGENTS = {"planner", "coder"}


class RoutedProvider(BaseProvider):
    """fast: intake/recovery/review/context. strong: planner/coder on Lane C or after repeated failures.
    Everything else uses the default model."""

    def __init__(self, default: BaseProvider, task: TaskState, fast: BaseProvider | None = None, strong: BaseProvider | None = None) -> None:
        self.default, self.fast, self.strong, self.task = default, fast, strong, task
        self.name = default.name
        self.model = default.model

    def pick(self, agent: str) -> BaseProvider:
        if self.fast and agent in FAST_AGENTS:
            return self.fast
        hard = self.task.lane == Lane.C or len(self.task.history) >= 2
        if self.strong and agent in STRONG_AGENTS and hard:
            return self.strong
        return self.default

    def chat(self, messages, *, tools=None, max_tokens=4096, agent="") -> ChatResult:
        return self.pick(agent).chat(messages, tools=tools, max_tokens=max_tokens, agent=agent)


def maybe_route(provider: BaseProvider, task: TaskState) -> BaseProvider:
    """Wrap with routing when model_fast / model_strong are configured (lcc.config.toml or env) for an OpenAI-compatible provider."""
    if not isinstance(provider, OpenAICompatibleProvider):
        return provider
    from lcc.config import load_config

    cfg = load_config().model
    fast_model = cfg.model_fast
    strong_model = cfg.model_strong
    if not fast_model and not strong_model:
        return provider

    def clone(model: str | None) -> BaseProvider | None:
        if not model:
            return None
        return provider.with_model(model)

    return RoutedProvider(provider, task, fast=clone(fast_model), strong=clone(strong_model))
