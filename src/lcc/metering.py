from __future__ import annotations

import time
from typing import Any

from lcc.model import BaseProvider, ChatResult
from lcc.schemas import TaskState
from lcc.store import HarnessStore


class MeteredProvider(BaseProvider):
    """Wraps a provider: charges every call to the task budget and logs a MODEL_CALL event."""

    def __init__(self, inner: BaseProvider, store: HarnessStore, task: TaskState, started: float | None = None) -> None:
        self.inner = inner
        self.store = store
        self.task = task
        self.started = started if started is not None else time.monotonic()
        self.name = inner.name
        self.model = inner.model

    def chat(self, messages: list[dict[str, Any]], *, tools=None, max_tokens=4096, agent="", **kw) -> ChatResult:
        t0 = time.monotonic()
        res = self.inner.chat(messages, tools=tools, max_tokens=max_tokens, agent=agent, **kw)
        b = self.task.budget
        b.record_tokens(res.usage.total)
        b.tokens_in += res.usage.input_tokens
        b.tokens_out += res.usage.output_tokens
        b.tokens_cached += res.usage.cached_tokens
        b.model_calls += 1
        self.tick()
        self.store.emit(
            self.task,
            "MODEL_CALL",
            agent=agent or self.task.current_agent,
            model=res.model or self.model,
            tokens_in=res.usage.input_tokens,
            tokens_out=res.usage.output_tokens,
            tokens_cached=res.usage.cached_tokens,
            tool_calls=[c.name for c in res.tool_calls],
            latency_s=round(time.monotonic() - t0, 2),
        )
        return res

    def tick(self) -> None:
        self.task.budget.runtime_used_seconds = round(time.monotonic() - self.started, 2)
