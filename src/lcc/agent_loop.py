"""Agent-computer interface loop: model -> tool calls -> bounded observations -> model."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from lcc.model import BaseProvider
from lcc.schemas import Budget
from lcc.tools import WRITE_TOOLS, ToolBudgetExceeded, ToolError, ToolRegistry

MAX_OBSERVATION_CHARS = 5000
CONTEXT_TOKEN_LIMIT = 16_000  # per-agent working context before old observations are compacted
KEEP_RECENT_OBSERVATIONS = 5
LOW_STEPS_WARNING = 3
NUDGE = "You must act through tools. Continue the task, or call `finish` with a summary if you are done."
READ_TOOLS = {"read_file"}


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
    context_limit: int = CONTEXT_TOKEN_LIMIT,
) -> LoopResult:
    allowed = [n for n in dict.fromkeys(allowed + ["finish"]) if n in tools.specs]
    schemas = tools.schemas(allowed)
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    seen: dict[str, int] = {}
    write_version = 0
    result = LoopResult(finished=False, messages=messages)
    idle_turns = 0
    reads: dict[str, list[int]] = {}  # path -> indexes of read_file observations still in the transcript

    for step in range(1, max_steps + 1):
        result.steps = step
        if budget.exhausted():
            result.stop_reason = "budget_exceeded"
            return result
        if step == max_steps - LOW_STEPS_WARNING and messages[-1]["role"] == "tool":
            messages[-1]["content"] += (f"\n\n[harness] {LOW_STEPS_WARNING} steps left: finish the fix and its test now, "
                                        "then call finish.")
        compact(messages, context_limit)
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
            if call.name in READ_TOOLS and succeeded:
                path = str(call.arguments.get("path") or "")
                _mark_stale(messages, [i for i in reads.get(path, []) if _same_range(messages[i], call.arguments)],
                            "superseded by a later read of the same lines")
                reads.setdefault(path, []).append(len(messages))
            if observation.startswith("BUDGET_EXCEEDED"):
                messages.append({"role": "tool", "tool_call_id": call.id, "content": observation})
                result.stop_reason = "tool_budget_exceeded"
                return result
            messages.append({"role": "tool", "tool_call_id": call.id, "content": observation})

    result.stop_reason = "max_steps"
    return result


def _mark_stale(messages: list[dict[str, Any]], indexes: list[int], why: str) -> None:
    """Old file contents are both wasted tokens and a trap (the model edits against stale text)."""
    for i in indexes:
        content = str(messages[i]["content"])
        if not content.startswith("[stale") and not content.startswith("[compacted"):
            first = content.splitlines()[0][:120] if content else ""
            messages[i]["content"] = f"[stale: {why}] {first}"


def _same_range(message: dict[str, Any], args: dict[str, Any]) -> bool:
    """True when a later read covers at least the lines of this earlier one."""
    content = str(message.get("content") or "")
    nums = [int(n) for n in re.findall(r"^\s*(\d+)\| ", content, re.M)]
    if not nums:
        return False
    start = int(args.get("start") or 1)
    end = int(args.get("end") or start + 199)
    return start <= min(nums) and end >= max(nums)


def _tokens(messages: list[dict[str, Any]]) -> int:
    return sum(len(str(m.get("content") or "")) + len(str(m.get("tool_calls") or "")) for m in messages) // 4


def compact(messages: list[dict[str, Any]], limit: int, keep: int = KEEP_RECENT_OBSERVATIONS) -> int:
    """Replace old tool observations with one-line stubs once the working context exceeds `limit` tokens.
    Deterministic (no model call) and keeps tool_call/tool pairing intact. Returns the number compacted."""
    if _tokens(messages) <= limit:
        return 0
    tool_idx = [i for i, m in enumerate(messages) if m["role"] == "tool" and not str(m["content"]).startswith("[compacted")]
    n = 0
    for i in tool_idx[:-keep] if keep else tool_idx:
        content = str(messages[i]["content"])
        first = content.splitlines()[0][:160] if content else ""
        messages[i]["content"] = f"[compacted {len(content)} chars; re-run the tool if needed] {first}"
        n += 1
        if _tokens(messages) <= limit:
            break
    return n


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
    if "tests_run" in out:
        return _render_test(out)
    parts: list[str] = []
    for k, v in out.items():
        if isinstance(v, str) and ("\n" in v or len(v) > 200):
            parts.append(f"{k}:\n{v}")
        elif isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            parts.append(f"{k}:\n" + "\n".join(v))
        else:
            parts.append(f"{k}: {json.dumps(v)}")
    return "\n".join(parts)


def _render_test(out: dict[str, Any]) -> str:
    """A passing run needs one line; a failing run needs the failures, not the progress dots."""
    head = (f"cmd: {out.get('cmd')}\nreturncode: {out.get('returncode')}  tests_run: {out.get('tests_run')}  "
            f"tests_failed: {out.get('tests_failed')}")
    if out.get("ok"):
        return head + "\nresult: all tests passed"
    ids = out.get("failed_ids") or []
    body = f"{out.get('stdout') or ''}\n{out.get('stderr') or ''}".strip()
    lines = [line for line in body.splitlines() if line.strip() and not re.fullmatch(r"[.sxF E%\[\]0-9]+", line.strip())]
    body = "\n".join(lines)
    return head + (f"\nfailed: {ids[:15]}" if ids else "") + "\n" + (body[-3500:] if len(body) > 3500 else body)
