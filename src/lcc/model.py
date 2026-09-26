from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class ProviderError(RuntimeError):
    pass


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = field(default_factory=lambda: "call_" + uuid.uuid4().hex[:12])


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class ChatResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""

    def assistant_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.text or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                for c in self.tool_calls
            ]
        return msg


class ModelProvider(Protocol):
    name: str
    model: str

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        agent: str = "",
    ) -> ChatResult: ...

    def structured_output(
        self, prompt: str, *, system: str = "", schema_hint: str = "", agent: str = ""
    ) -> dict[str, Any]: ...


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def parse_json_object(raw: str) -> dict[str, Any] | None:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1]
        if raw.rstrip().endswith("```"):
            raw = raw.rstrip()[:-3]
    for candidate in (raw, raw[raw.find("{") : raw.rfind("}") + 1] if "{" in raw else ""):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return data if isinstance(data, dict) else {"value": data}
    return None


class BaseProvider:
    """Shared helpers built on top of `chat`."""

    name = "base"
    model = "none"

    def chat(self, messages, *, tools=None, max_tokens=4096, agent=""):  # pragma: no cover - abstract
        raise NotImplementedError

    def complete(self, prompt: str, *, system: str = "", max_tokens: int = 2048, agent: str = "") -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        return self.chat(messages, max_tokens=max_tokens, agent=agent).text

    def structured_output(self, prompt: str, *, system: str = "", schema_hint: str = "", agent: str = "") -> dict[str, Any]:
        sys = f"{system}\nReturn ONLY one valid JSON object, no prose. Shape: {schema_hint}".strip()
        messages = [{"role": "system", "content": sys}, {"role": "user", "content": prompt}]
        res = self.chat(messages, max_tokens=4096, agent=agent)
        data = parse_json_object(res.text)
        if data is not None:
            return data
        # One repair attempt: show the model its own output.
        messages += [
            {"role": "assistant", "content": res.text},
            {"role": "user", "content": "That was not valid JSON. Reply with ONLY the JSON object."},
        ]
        res = self.chat(messages, max_tokens=4096, agent=agent)
        return parse_json_object(res.text) or {"text": res.text}


class MockProvider(BaseProvider):
    """Deterministic offline backend. Keyword heuristics only; used for demos and smoke tests."""

    name = "mock"
    model = "mock"

    def chat(self, messages, *, tools=None, max_tokens=4096, agent=""):
        system = next((m["content"] or "" for m in messages if m["role"] == "system"), "")
        prompt = "\n".join(str(m.get("content") or "") for m in messages if m["role"] == "user")
        usage = Usage(sum(estimate_tokens(str(m.get("content") or "")) for m in messages), 50)
        if tools:
            return ChatResult(tool_calls=self._tool_calls(agent, messages, prompt), usage=usage, model=self.model)
        return ChatResult(text=self._text(system, prompt), usage=usage, model=self.model)

    def _tool_calls(self, agent: str, messages: list[dict[str, Any]], prompt: str) -> list[ToolCall]:
        already_acted = any(m["role"] == "tool" for m in messages)
        if agent == "coder" and not already_acted and "add" in prompt.lower():
            return [ToolCall("write_file", {"path": "app.py", "content": "def add(a, b):\n    return a + b\n"})]
        return [ToolCall("finish", {"summary": "mock finished"})]

    def _text(self, system: str, prompt: str) -> str:
        s = system.lower()
        if "issue analyst" in s:
            return json.dumps(
                {
                    "problem": "Implement the requested change.",
                    "intent": "Satisfy the issue with a minimal patch.",
                    "requirements": ["Meet stated acceptance criteria"],
                    "acceptance_criteria": [{"id": "AC-01", "text": "Tests pass"}],
                    "constraints": ["Do not modify main directly"],
                    "ambiguities": [],
                    "blocking_ambiguities": [],
                    "risk": "low",
                }
            )
        if "planner" in s:
            files = ["app.py", "tests/test_app.py"] if "add" in prompt.lower() else []
            return json.dumps(
                {
                    "steps": [{"order": 1, "action": "Implement requested behavior", "files": files, "verification": "unit tests"}],
                    "allowed_files": files,
                    "forbidden_files": [],
                }
            )
        if "adversarial reviewer" in s:
            return json.dumps({"findings": []})
        if "recovery agent" in s:
            return json.dumps({"class": "CODE_BUG", "action": "patch", "notes": "retry with tighter patch"})
        return json.dumps({"ok": True})


def tc(name: str, **arguments: Any) -> ToolCall:
    """Test helper: build a tool call."""
    return ToolCall(name, arguments)


class ScriptedProvider(BaseProvider):
    """Replays scripted responses per agent; falls back to MockProvider. Records every request."""

    name = "scripted"
    model = "scripted"

    def __init__(self, script: dict[str, list[Any]] | None = None, fallback: BaseProvider | None = None) -> None:
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.fallback = fallback or MockProvider()
        self.seen: list[tuple[str, list[dict[str, Any]]]] = []

    def chat(self, messages, *, tools=None, max_tokens=4096, agent=""):
        self.seen.append((agent, [dict(m) for m in messages]))
        queue = self.script.get(agent)
        if queue:
            item = queue.pop(0)
            usage = Usage(sum(estimate_tokens(str(m.get("content") or "")) for m in messages), 40)
            if isinstance(item, ChatResult):
                return item
            if isinstance(item, str):
                return ChatResult(text=item, usage=usage, model=self.model)
            if isinstance(item, dict) and ("text" in item or "tool_calls" in item):
                calls = [c if isinstance(c, ToolCall) else ToolCall(c["name"], c.get("arguments", {})) for c in item.get("tool_calls", [])]
                return ChatResult(text=item.get("text", ""), tool_calls=calls, usage=usage, model=self.model)
            if isinstance(item, list):
                calls = [c if isinstance(c, ToolCall) else ToolCall(c["name"], c.get("arguments", {})) for c in item]
                return ChatResult(tool_calls=calls, usage=usage, model=self.model)
            return ChatResult(text=json.dumps(item), usage=usage, model=self.model)
        return self.fallback.chat(messages, tools=tools, max_tokens=max_tokens, agent=agent)

    def prompts_for(self, agent: str) -> list[str]:
        return ["\n".join(str(m.get("content") or "") for m in msgs) for a, msgs in self.seen if a == agent]


# name -> (base_url, default model, api-key env var)
PRESETS: dict[str, tuple[str, str, str]] = {
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat", "DEEPSEEK_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.5-flash", "GEMINI_API_KEY"),
    "openai": ("https://api.openai.com/v1", "gpt-4.1", "OPENAI_API_KEY"),
    "grok": ("https://api.x.ai/v1", "grok-4", "XAI_API_KEY"),
}


class OpenAICompatibleProvider(BaseProvider):
    """Chat-completions + function calling. Covers DeepSeek, Gemini (OpenAI-compat endpoint), OpenAI, xAI."""

    def __init__(
        self,
        preset: str = "openai",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_retries: int = 4,
        timeout: float = 180,
    ) -> None:
        if preset not in PRESETS:
            raise ProviderError(f"unknown provider preset {preset!r}; choose from {sorted(PRESETS)}")
        default_url, default_model, key_env = PRESETS[preset]
        self.name = preset
        self.api_key = api_key or os.environ.get("LCC_API_KEY") or os.environ.get(key_env) or ""
        if not self.api_key:
            raise ProviderError(f"{preset}: no API key. Set {key_env} (or LCC_API_KEY).")
        self.base_url = (base_url or os.environ.get("LCC_BASE_URL") or default_url).rstrip("/")
        self.model = model or os.environ.get("LCC_MODEL") or default_model
        self.max_retries = max_retries
        self.timeout = timeout

    def chat(self, messages, *, tools=None, max_tokens=4096, agent=""):
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.1}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = self._post(payload)
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise ProviderError(f"{self.name}: malformed response: {str(data)[:500]}") from exc
        calls: list[ToolCall] = []
        for raw in msg.get("tool_calls") or []:
            fn = raw.get("function") or {}
            args_raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            except json.JSONDecodeError:
                args = {"__invalid_json__": args_raw}
            calls.append(ToolCall(fn.get("name") or "", args if isinstance(args, dict) else {}, raw.get("id") or "call_" + uuid.uuid4().hex[:12]))
        u = data.get("usage") or {}
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or u.get("prompt_cache_hit_tokens") or 0
        usage = Usage(int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0), int(cached or 0))
        return ChatResult(text=msg.get("content") or "", tool_calls=calls, usage=usage, model=data.get("model") or self.model)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        delay = 2.0
        last = ""
        for attempt in range(self.max_retries):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    r = client.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json=payload,
                    )
            except httpx.HTTPError as exc:
                last = f"transport error: {exc}"
            else:
                if r.status_code == 200:
                    return r.json()
                last = f"HTTP {r.status_code}: {r.text[:500]}"
                if r.status_code not in {408, 409, 429} and r.status_code < 500:
                    raise ProviderError(f"{self.name}: {last}")
            if attempt < self.max_retries - 1:
                time.sleep(delay)
                delay *= 2
        raise ProviderError(f"{self.name}: gave up after {self.max_retries} attempts; {last}")


def get_provider(name: str | None = None) -> BaseProvider:
    kind = (name or os.environ.get("LCC_PROVIDER") or "mock").lower()
    if kind == "mock":
        return MockProvider()
    if kind in {"openai_compat", "xai"}:
        kind = {"xai": "grok"}.get(kind, "openai")
    return OpenAICompatibleProvider(kind)
