"""Agent-computer interface loop: model -> tool calls -> bounded observations -> model."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from lcc.model import BaseProvider
from lcc.schemas import Budget
from lcc.tools import WRITE_TOOLS, ToolBudgetExceeded, ToolError, ToolRegistry

MAX_OBSERVATION_CHARS = 6000
NUDGE = "You must act through tools. Continue the task, or call `finish` with a summary if you are done."


@dataclass
class LoopResult:
    finished: bool
    summary: str = ""
    stop_reason: str = ""
    steps: int = 0
    tool_calls: int = 0
    finish_args: dict[str, Any] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)


def truncate(text: str, limit: int = MAX_OBSERVATION_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    return f"{text[:head]}\n[... truncated {len(text) - limit} chars ...]\n{text[-tail:]}"


def run_tool_loop(
    provider: BaseProvider,
    *,
    agent: str,
    system: str,
    prompt: str,
    tools: ToolRegistry,
    allowed: list[str],
    budget: Budget,
    max_steps: int = 30,
    max_tokens: int = 4096,
) -> LoopResult:
    allowed = [n for n in dict.fromkeys(allowed + ["finish"]) if n in tools.specs]
    schemas = tools.schemas(allowed)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    seen: dict[str, int] = {}
    write_version = 0
    result = LoopResult(finished=False, messages=messages)
    idle_turns = 0

    for step in range(1, max_steps + 1):
        result.steps = step
        if budget.exhausted():
            result.stop_reason = "budget_exceeded"
            return result
        reply = provider.chat(messages, tools=schemas, max_tokens=max_tokens, agent=agent)
        messages.append(reply.assistant_message())

        if not reply.tool_calls:
            idle_turns += 1
            if idle_turns >= 2:
                result.finished = True
                result.summary = reply.text
                result.stop_reason = "no_tool_calls"
                return result
            messages.append({"role": "user", "content": NUDGE})
            continue
        idle_turns = 0

        for call in reply.tool_calls:
            result.tool_calls += 1
            if call.name == "finish":
                result.finished = True
                result.finish_args = dict(call.arguments)
                result.summary = str(call.arguments.get("summary") or "")
                result.stop_reason = "finished"
                return result
            key = f"{write_version}|{call.name}|{json.dumps(call.arguments, sort_keys=True)}"
            seen[key] = seen.get(key, 0) + 1
            if seen[key] >= 3:
                result.stop_reason = "repeated_tool_call"
                return result
            observation, succeeded = _execute(tools, call.name, call.arguments, allowed)
            if seen[key] == 2:
                observation = (
                    "WARNING: you already made this exact call and nothing has changed since. "
                    "Do something different.\n" + observation
                )
            if call.name in WRITE_TOOLS and succeeded:
                write_version += 1
            if observation.startswith("BUDGET_EXCEEDED"):
                messages.append({"role": "tool", "tool_call_id": call.id, "content": observation})
                result.stop_reason = "tool_budget_exceeded"
                return result
            messages.append({"role": "tool", "tool_call_id": call.id, "content": observation})

    result.stop_reason = "max_steps"
    return result


def _execute(tools: ToolRegistry, name: str, args: dict[str, Any], allowed: list[str]) -> tuple[str, bool]:
    if name not in allowed:
        return f"ERROR: tool `{name}` is not available to this agent. Available: {', '.join(allowed)}", False
    if "__invalid_json__" in args:
        return "ERROR: tool arguments were not valid JSON. Retry with valid JSON arguments.", False
    try:
        out = tools.call(name, **args)
    except ToolBudgetExceeded as exc:
        return f"BUDGET_EXCEEDED: {exc}", False
    except ToolError as exc:
        return f"ERROR: {exc}", False
    except TypeError as exc:
        return f"ERROR: bad arguments for {name}: {exc}", False
    except OSError as exc:
        return f"ERROR: {exc}", False
    return truncate(_render(out)), out.get("ok") is not False


def _render(out: dict[str, Any]) -> str:
    """Render tool output compactly: long text fields are shown raw rather than JSON-escaped."""
    parts: list[str] = []
    for k, v in out.items():
        if isinstance(v, str) and ("\n" in v or len(v) > 200):
            parts.append(f"{k}:\n{v}")
        elif isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            parts.append(f"{k}:\n" + "\n".join(v))
        else:
            parts.append(f"{k}: {json.dumps(v)}")
    return "\n".join(parts)
