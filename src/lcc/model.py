from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


class ProviderError(RuntimeError):
    """`fatal` errors (bad key, no balance, model missing) fail every later call too: stop instead of retrying."""

    def __init__(self, message: str, *, fatal: bool = False, status: int = 0) -> None:
        super().__init__(message)
        self.fatal = fatal
        self.status = status


class _UseTextTools(Exception):
    """The server rejected native function calling; re-send with the tools described in the prompt."""


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
        # "" rather than None: several OpenAI-compatible servers (DashScope, vLLM builds) reject null content.
        msg: dict[str, Any] = {"role": "assistant", "content": self.text or ""}
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


THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.S)


def strip_thinking(text: str) -> str:
    """Reasoning models served raw (Qwen3, DeepSeek-R1 on vLLM/Ollama) put their chain of thought in the content."""
    text = THINK_BLOCK.sub("", text or "")
    if "</think>" in text:  # opening tag consumed by the chat template
        text = text.split("</think>", 1)[1]
    return text.strip()


def parse_json_object(raw: str) -> dict[str, Any] | None:
    raw = strip_thinking(raw)
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


TEXT_TOOL_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_text_tool_calls(text: str, names: set[str]) -> list[ToolCall]:
    """Tool calls written into the message text: Qwen/Hermes `<tool_call>{...}</tool_call>` blocks, or a fenced
    JSON object with `name` + `arguments`. Used when a server has no native function calling."""
    calls: list[ToolCall] = []
    blobs = TEXT_TOOL_CALL.findall(text or "") or FENCED_JSON.findall(text or "")
    if not blobs and (text or "").strip().startswith("{"):
        blobs = [text.strip()]
    for blob in blobs:
        try:
            data = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("name") or (data.get("function") or {}).get("name")
        args = data.get("arguments", data.get("parameters", (data.get("function") or {}).get("arguments", {})))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"__invalid_json__": args}
        if name in names and isinstance(args, dict):
            calls.append(ToolCall(name, args))
    return calls


class BaseProvider:
    """Shared helpers built on top of `chat`."""

    name = "base"
    model = "none"
    json_mode = False

    def chat(self, messages, *, tools=None, max_tokens=4096, agent="", **kw):  # pragma: no cover - abstract
        raise NotImplementedError

    def complete(self, prompt: str, *, system: str = "", max_tokens: int = 2048, agent: str = "") -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        return self.chat(messages, max_tokens=max_tokens, agent=agent).text

    def structured_output(self, prompt: str, *, system: str = "", schema_hint: str = "", agent: str = "",
                          max_tokens: int = 2048) -> dict[str, Any]:
        sys = f"{system}\nReturn ONLY one valid JSON object, no prose. Shape: {schema_hint}".strip()
        messages = [{"role": "system", "content": sys}, {"role": "user", "content": prompt}]
        res = self._json_chat(messages, max_tokens, agent)
        data = parse_json_object(res.text)
        if data is not None:
            return data
        # One repair attempt: show the model its own output.
        messages += [
            {"role": "assistant", "content": res.text},
            {"role": "user", "content": "That was not valid JSON. Reply with ONLY the JSON object."},
        ]
        res = self._json_chat(messages, max_tokens, agent)
        return parse_json_object(res.text) or {"text": res.text}

    def _json_chat(self, messages, max_tokens, agent) -> ChatResult:
        return self.chat(messages, max_tokens=max_tokens, agent=agent, response_format={"type": "json_object"})

    def preflight(self) -> str:
        """Cheap check that the backend answers. Offline providers always pass."""
        return self.model


class MockProvider(BaseProvider):
    """Deterministic offline backend. Keyword heuristics only; used for demos and smoke tests."""

    name = "mock"
    model = "mock"

    def chat(self, messages, *, tools=None, max_tokens=4096, agent="", **kw):
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

    def chat(self, messages, *, tools=None, max_tokens=4096, agent="", **kw):
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


@dataclass(frozen=True)
class Preset:
    base_url: str
    model: str
    # Tried in order when `model` is unknown to the account (HTTP 404 / model-not-found) during preflight.
    fallbacks: tuple[str, ...] = ()
    seed: bool = False  # the endpoint accepts `seed`
    json_mode: bool = False  # the endpoint accepts response_format={"type":"json_object"}
    parallel_tools: bool = False  # send parallel_tool_calls=true (fewer round trips)


# The final evaluation uses DeepSeek and Qwen models; those two come first and are the only ones an
# ambiguous `sk-` key is ever sent to.
PRESETS: dict[str, Preset] = {
    "deepseek": Preset("https://api.deepseek.com/v1", "deepseek-chat", ("deepseek-chat", "deepseek-reasoner"),
                       seed=True, json_mode=True),
    # Alibaba Cloud Model Studio (DashScope), international and mainland-China endpoints.
    "qwen": Preset("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "qwen3-coder-plus",
                   ("qwen3-coder-plus", "qwen-plus", "qwen-max", "qwen-turbo"), seed=True, json_mode=True, parallel_tools=True),
    "qwen-cn": Preset("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3-coder-plus",
                      ("qwen3-coder-plus", "qwen-plus", "qwen-max", "qwen-turbo"), seed=True, json_mode=True, parallel_tools=True),
    "gemini": Preset("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-3.8-flash",
                     ("gemini-3.8-flash", "gemini-2.5-flash")),
    "openai": Preset("https://api.openai.com/v1", "gpt-4.1", seed=True, json_mode=True, parallel_tools=True),
    "anthropic": Preset("https://api.anthropic.com/v1", "claude-sonnet-5"),
    "openrouter": Preset("https://openrouter.ai/api/v1", "deepseek/deepseek-chat", seed=True),
    "groq": Preset("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", seed=True),
    "grok": Preset("https://api.x.ai/v1", "grok-4", seed=True),
    "custom": Preset("", ""),
}
ALIASES = {"xai": "grok", "openai_compat": "openai", "claude": "anthropic", "dashscope": "qwen",
           "dashscope-cn": "qwen-cn", "alibaba": "qwen", "local": "custom", "ollama": "custom", "vllm": "custom"}

# Unambiguous key prefixes. A bare `sk-` key is shared by DeepSeek and DashScope and is resolved by probing.
KEY_PREFIXES: list[tuple[str, str]] = [
    ("sk-ant-", "anthropic"),
    ("sk-or-", "openrouter"),
    ("sk-proj-", "openai"),
    ("sk-svcacct-", "openai"),
    ("AIza", "gemini"),
    ("AQ.", "gemini"),
    ("xai-", "grok"),
    ("gsk_", "groq"),
]
PROBE_ORDER = ["deepseek", "qwen", "qwen-cn"]
PLACEHOLDER_KEY = re.compile(r"^(?:<.*>|your[-_ ]?(?:api[-_ ]?)?key.*|changeme|xxx+|\.\.\.|placeholder|todo)$", re.I)
_DETECTED: dict[str, str] = {}

ACCOUNT_ERRORS = {
    401: "the API key was rejected (401). Check AI_API_KEY.",
    402: "the account has insufficient balance (402). Top up the account behind AI_API_KEY.",
    403: "the key is not allowed to use this model or endpoint (403).",
    404: "model or endpoint not found (404). Set [model].model in lcc.config.toml or LCC_MODEL.",
}
QUOTA_WORDS = ("insufficient_quota", "insufficient balance", "arrearage", "quota exceeded", "billing")
MODEL_MISSING = re.compile(r"model[_ ]not[_ ]found|model.{0,40}(?:does not exist|not exist|not found|not supported|invalid)", re.I)


def check_key(key: str) -> None:
    if PLACEHOLDER_KEY.match(key.strip()):
        raise ProviderError("AI_API_KEY is set to a placeholder, not a real key. Export the real key.", fatal=True)


def detect_provider(key: str, base_url: str = "", probe: bool = True) -> str:
    """Pick a preset from the credential's format. An ambiguous `sk-` key is checked with `GET /models`
    (no tokens spent) against DeepSeek and DashScope only, and the answer is cached for the process."""
    if base_url:
        return "custom"
    for prefix, name in KEY_PREFIXES:
        if key.startswith(prefix):
            return name
    if not probe:
        return PROBE_ORDER[0]
    digest = hashlib.sha256(key.encode()).hexdigest()
    if digest in _DETECTED:
        return _DETECTED[digest]
    for name in PROBE_ORDER:
        try:
            r = httpx.get(f"{PRESETS[name].base_url}/models", headers={"Authorization": f"Bearer {key}"}, timeout=15)
        except httpx.HTTPError:
            continue
        if r.status_code == 200:
            _DETECTED[digest] = name
            return name
    raise ProviderError(
        "could not identify the provider for AI_API_KEY (neither DeepSeek nor Qwen/DashScope accepted it). "
        "Set LCC_PROVIDER (deepseek | qwen | qwen-cn | openai | custom ...) or LCC_BASE_URL.",
        fatal=True,
    )


class OpenAICompatibleProvider(BaseProvider):
    """Text-only chat-completions + function calling, one adapter for every preset. It adapts to the server:
    parameters a server rejects are dropped and retried once, and servers without function calling get the
    tools described in the prompt with `<tool_call>` blocks parsed from the reply."""

    def __init__(
        self,
        preset: str = "openai",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        default_model: str | None = None,
        temperature: float = 0.0,
        seed: int | None = None,
        max_retries: int = 4,
        timeout: float = 180,
    ) -> None:
        preset = ALIASES.get(preset, preset)
        if preset not in PRESETS:
            raise ProviderError(f"unknown provider preset {preset!r}; choose from {sorted(PRESETS)}", fatal=True)
        from lcc.config import api_key as env_key

        p = PRESETS[preset]
        self.name = preset
        self.preset = p
        self.api_key = api_key or env_key()
        if not self.api_key and preset != "custom":
            raise ProviderError(f"{preset}: no API key. Export AI_API_KEY.", fatal=True)
        self.base_url = (base_url or os.environ.get("LCC_BASE_URL") or p.base_url).rstrip("/")
        self.model = model or os.environ.get("LCC_MODEL") or default_model or p.model
        if not self.base_url or not self.model:
            raise ProviderError(f"{preset}: base_url and model must be set in lcc.config.toml", fatal=True)
        self.model_pinned = bool(model or os.environ.get("LCC_MODEL"))
        self.temperature = temperature
        self.seed = seed if p.seed else None
        self.json_mode = p.json_mode
        self.parallel_tools = p.parallel_tools
        self.native_tools = True
        self.extra: dict[str, Any] = {}
        self.max_retries = max_retries
        self.timeout = timeout
        self._client: httpx.Client | None = None

    def with_model(self, model: str) -> "OpenAICompatibleProvider":
        clone = OpenAICompatibleProvider(
            self.name, api_key=self.api_key, base_url=self.base_url, model=model,
            temperature=self.temperature, seed=self.seed, max_retries=self.max_retries, timeout=self.timeout,
        )
        clone.native_tools, clone.extra, clone.json_mode = self.native_tools, dict(self.extra), self.json_mode
        return clone

    # ------------------------------------------------------------ calls
    def chat(self, messages, *, tools=None, max_tokens=4096, agent="", response_format=None):
        text_tools = bool(tools) and not self.native_tools
        sent = _text_mode(messages, tools) if text_tools else messages
        payload: dict[str, Any] = {"model": self.model, "messages": sent, "max_tokens": max_tokens,
                                   "temperature": self.temperature, **self.extra}
        if self.seed is not None:
            payload["seed"] = self.seed
        if tools and not text_tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
            if self.parallel_tools:
                payload["parallel_tool_calls"] = True
        if response_format and self.json_mode:
            payload["response_format"] = response_format
        try:
            data = self._post(payload)
        except _UseTextTools:
            return self.chat(messages, tools=tools, max_tokens=max_tokens, agent=agent, response_format=response_format)
        try:
            msg = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
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
        text = strip_thinking(msg.get("content") or "")
        if tools and not calls:
            names = {t["function"]["name"] for t in tools}
            calls = extract_text_tool_calls(text, names)
            if calls:
                text = TEXT_TOOL_CALL.sub("", text).strip()
        u = data.get("usage") or {}
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens") or u.get("prompt_cache_hit_tokens") or 0
        usage = Usage(int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0), int(cached or 0))
        return ChatResult(text=text, tool_calls=calls, usage=usage, model=data.get("model") or self.model)

    def preflight(self) -> str:
        """One tiny completion before any work, so a bad key, an empty balance or an unknown model is reported
        in seconds. An unknown default model falls back to the next one the account serves."""
        candidates = [self.model] if self.model_pinned else list(dict.fromkeys([self.model, *self.preset.fallbacks]))
        last: ProviderError | None = None
        for model in candidates:
            self.model = model
            try:
                self.chat([{"role": "user", "content": "Reply with the word ok."}], max_tokens=8)
                return model
            except ProviderError as exc:
                last = exc
                if not (exc.status == 404 or _model_missing(str(exc))):
                    raise
        raise last or ProviderError(f"{self.name}: no usable model", fatal=True)

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout)
        return self._client

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        delay = 2.0
        last = ""
        adapted: set[str] = set()
        attempt = 0
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        while attempt < self.max_retries:
            attempt += 1
            try:
                r = self._http().post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            except httpx.HTTPError as exc:
                last = f"transport error: {exc}"
            else:
                if r.status_code == 200:
                    try:
                        return r.json()
                    except ValueError:
                        last = f"non-JSON response: {r.text[:300]}"
                else:
                    body = r.text[:800]
                    last = f"HTTP {r.status_code}: {body}"
                    fix = self._adapt(r.status_code, body, payload, adapted)
                    if fix == "tools":
                        raise _UseTextTools()
                    if fix:
                        adapted.add(fix)
                        attempt -= 1  # an adaptation is not a failed attempt
                        continue
                    low = body.lower()
                    out_of_quota = any(w in low for w in QUOTA_WORDS) and (r.status_code != 429 or "insufficient" in low)
                    missing = r.status_code in {400, 404} and _model_missing(body)
                    if r.status_code in ACCOUNT_ERRORS or out_of_quota or missing:
                        hint = ACCOUNT_ERRORS.get(r.status_code) or (
                            "model not found. Set [model].model or LCC_MODEL." if missing
                            else "the account is out of quota or balance.")
                        raise ProviderError(f"{self.name}/{self.model}: {hint} Server said: {body[:300]}",
                                            fatal=True, status=404 if missing else r.status_code)
                    if r.status_code not in {408, 409, 429} and r.status_code < 500:
                        raise ProviderError(f"{self.name}: {last}")
                    wait = r.headers.get("retry-after")
                    if wait and wait.replace(".", "", 1).isdigit():
                        delay = max(delay, min(float(wait), 60.0))
            if attempt < self.max_retries:
                time.sleep(delay)
                delay *= 2
        raise ProviderError(f"{self.name}: gave up after {self.max_retries} attempts; {last}")

    def _adapt(self, status: int, body: str, payload: dict[str, Any], done: set[str]) -> str:
        """Drop or fix a parameter the server rejected (HTTP 400/422). Returns what was changed, or ''."""
        if status not in {400, 422}:
            return ""
        low = body.lower()
        if "enable_thinking" in low and "enable_thinking" not in done:
            self.extra["enable_thinking"] = False  # Qwen3 on DashScope: required for non-streaming calls
            payload["enable_thinking"] = False
            return "enable_thinking"
        for key in ("response_format", "parallel_tool_calls", "seed"):
            if key in payload and key.replace("_", " ") in low.replace("_", " ") and key not in done:
                payload.pop(key)
                if key == "response_format":
                    self.json_mode = False
                elif key == "parallel_tool_calls":
                    self.parallel_tools = False
                else:
                    self.seed = None
                return key
        if "tools" in payload and "tools" not in done and re.search(r"tool|function", low):
            self.native_tools = False  # chat() re-sends with the tools described in the prompt
            return "tools"
        if "max_tokens" in low and payload.get("max_tokens", 0) > 2048 and "max_tokens" not in done:
            payload["max_tokens"] = 2048
            return "max_tokens"
        return ""


def _model_missing(text: str) -> bool:
    return bool(MODEL_MISSING.search(text))


def _text_mode(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Describe the tools in the system prompt and turn tool traffic into plain turns, for servers
    without native function calling."""
    lines = []
    for t in tools or []:
        fn = t["function"]
        params = ", ".join(f"{k}{'' if k in fn['parameters'].get('required', []) else '?'}"
                           for k in fn["parameters"].get("properties", {}))
        lines.append(f"- {fn['name']}({params}): {fn['description']}")
    guide = (
        "\n\nYou can call tools. To call one, reply with one or more blocks exactly like\n"
        '<tool_call>{"name": "read_file", "arguments": {"path": "src/app.py"}}</tool_call>\n'
        "and nothing else; results come back in <tool_response> blocks. Available tools:\n" + "\n".join(lines)
    )
    out: list[dict[str, Any]] = []
    for m in messages:
        role, content = m["role"], str(m.get("content") or "")
        if role == "system":
            m = {"role": "system", "content": content + guide}
            guide = ""
        elif role == "assistant" and m.get("tool_calls"):
            blocks = "\n".join(
                f'<tool_call>{{"name": "{c["function"]["name"]}", "arguments": {c["function"]["arguments"]}}}</tool_call>'
                for c in m["tool_calls"])
            m = {"role": "assistant", "content": (content + "\n" + blocks).strip()}
        elif role == "tool":
            m = {"role": "user", "content": f"<tool_response>\n{content}\n</tool_response>"}
        else:
            m = {"role": role, "content": content}
        if out and out[-1]["role"] == m["role"] and m["role"] != "system":
            out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + m["content"]}
        else:
            out.append(m)
    if guide:  # no system message at all
        out.insert(0, {"role": "system", "content": guide.strip()})
    return out


def get_provider(name: str | None = None) -> BaseProvider:
    """Build the provider from `lcc.config.toml` + environment. `name` overrides the configured provider."""
    from lcc.config import api_key, load_config

    cfg = load_config().model
    kind = (name or cfg.provider or "auto").lower()
    if kind == "mock":
        return MockProvider()
    kind = ALIASES.get(kind, kind)
    key = api_key()
    if key:
        check_key(key)
    if kind == "auto":
        if not key and not cfg.base_url:
            raise ProviderError("no API key. Export AI_API_KEY (the harness never reads keys from committed files).", fatal=True)
        kind = detect_provider(key, cfg.base_url)
    return OpenAICompatibleProvider(
        kind, base_url=cfg.base_url or None, model=cfg.model or None, default_model=cfg.defaults.get(kind) or None,
        temperature=cfg.temperature, seed=cfg.seed,
    )
