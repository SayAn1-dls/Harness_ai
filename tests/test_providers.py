"""Provider behaviour against a fake OpenAI-compatible server (httpx.MockTransport): the paths DeepSeek, Qwen
(DashScope) and self-hosted models exercise, without a network or a key."""

import json

import httpx
import pytest

from lcc import model as model_mod
from lcc.bench import run_bench
from lcc.model import (
    OpenAICompatibleProvider,
    ProviderError,
    detect_provider,
    extract_text_tool_calls,
    get_provider,
    strip_thinking,
)

TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "read",
                                           "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                                                          "required": ["path"]}}}]


def _ok(content="ok", tool_calls=None, cached=0):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return httpx.Response(200, json={"choices": [{"message": msg}], "model": "m",
                                     "usage": {"prompt_tokens": 10, "completion_tokens": 2, "prompt_cache_hit_tokens": cached}})


def _provider(handler, preset="qwen", **kw):
    p = OpenAICompatibleProvider(preset, api_key="sk-test", max_retries=2, **kw)
    p._client = httpx.Client(transport=httpx.MockTransport(handler))
    return p


def test_placeholder_key_fails_fast(monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "your-key")
    monkeypatch.delenv("LCC_PROVIDER", raising=False)
    with pytest.raises(ProviderError, match="placeholder") as err:
        get_provider("auto")
    assert err.value.fatal


def test_dotenv_fills_empty_variables(monkeypatch, tmp_path):
    from lcc.cli import _load_dotenv

    env = tmp_path / ".env"
    env.write_text('AI_API_KEY="from-dotenv"\nLCC_MODEL=m1\n')
    monkeypatch.setenv("AI_API_KEY", "")  # what `make` passes when the variable is unset
    monkeypatch.setenv("LCC_MODEL", "explicit")
    _load_dotenv(env)
    import os

    assert os.environ["AI_API_KEY"] == "from-dotenv"
    assert os.environ["LCC_MODEL"] == "explicit"


@pytest.mark.parametrize("status,body", [(401, "invalid api key"), (402, "Insufficient Balance"), (403, "denied"),
                                         (400, '{"code":"Arrearage","message":"account overdue"}')])
def test_account_errors_are_fatal(status, body):
    p = _provider(lambda req: httpx.Response(status, text=body))
    with pytest.raises(ProviderError) as err:
        p.chat([{"role": "user", "content": "hi"}])
    assert err.value.fatal


def test_rate_limit_is_retried_not_fatal(monkeypatch):
    monkeypatch.setattr(model_mod.time, "sleep", lambda s: None)
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, text="rate limit, retry") if len(calls) == 1 else _ok("fine")

    assert _provider(handler).chat([{"role": "user", "content": "hi"}]).text == "fine"
    assert len(calls) == 2


def test_preflight_falls_back_to_a_served_model():
    tried = []

    def handler(req):
        m = json.loads(req.content)["model"]
        tried.append(m)
        if m == "qwen3-coder-plus":
            return httpx.Response(404, json={"error": {"code": "model_not_found", "message": "The model does not exist"}})
        return _ok()

    p = _provider(handler)
    assert p.preflight() == "qwen-plus"
    assert tried == ["qwen3-coder-plus", "qwen-plus"] and p.model == "qwen-plus"


def test_pinned_model_does_not_fall_back():
    p = _provider(lambda req: httpx.Response(404, text="model_not_found"), model="my-model")
    with pytest.raises(ProviderError) as err:
        p.preflight()
    assert err.value.fatal


def test_rejected_parameters_are_adapted():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        if "enable_thinking" not in body:
            return httpx.Response(400, text="parameter.enable_thinking must be set to false for non-streaming calls")
        if "response_format" in body:
            return httpx.Response(400, text="response_format is not supported by this model")
        return _ok('{"a": 1}')

    p = _provider(handler)
    assert p.structured_output("give json", schema_hint="{}") == {"a": 1}
    assert seen[-1]["enable_thinking"] is False and "response_format" not in seen[-1]
    assert p.extra == {"enable_thinking": False} and p.json_mode is False  # remembered for later calls


def test_server_without_function_calling_uses_text_tools():
    sent = []

    def handler(req):
        body = json.loads(req.content)
        sent.append(body)
        if "tools" in body:
            return httpx.Response(400, text='"auto" tool choice requires --enable-auto-tool-choice')
        assert all(m["role"] != "tool" for m in body["messages"])
        return _ok('<think>let me look</think>\n<tool_call>{"name": "read_file", "arguments": {"path": "a.py"}}</tool_call>')

    p = _provider(handler, preset="custom", base_url="http://localhost:8000/v1", model="qwen3-local")
    history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"},
               {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                "function": {"name": "read_file", "arguments": '{"path": "b.py"}'}}]},
               {"role": "tool", "tool_call_id": "c1", "content": "file b"}]
    res = p.chat(history, tools=TOOLS)
    assert [(c.name, c.arguments) for c in res.tool_calls] == [("read_file", {"path": "a.py"})]
    assert "read_file(path)" in sent[-1]["messages"][0]["content"] and not p.native_tools
    assert "<tool_response>" in sent[-1]["messages"][-1]["content"]


def test_reasoning_text_and_cached_tokens():
    p = _provider(lambda req: _ok("<think>hmm</think>answer", cached=7), preset="deepseek")
    res = p.chat([{"role": "user", "content": "q"}])
    assert res.text == "answer" and res.usage.cached_tokens == 7
    assert strip_thinking("reasoning...</think>final") == "final"
    assert extract_text_tool_calls('```json\n{"name": "read_file", "arguments": {"path": "x"}}\n```', {"read_file"})[0].arguments == {"path": "x"}


def test_ambiguous_key_is_probed_on_deepseek_and_qwen_only(monkeypatch):
    urls = []

    def fake_get(url, headers=None, timeout=None):
        urls.append(url)
        return httpx.Response(200 if "dashscope-intl" in url else 401)

    monkeypatch.setattr(model_mod.httpx, "get", fake_get)
    model_mod._DETECTED.clear()
    assert detect_provider("sk-0123456789abcdef") == "qwen"
    assert detect_provider("sk-0123456789abcdef") == "qwen"  # cached: no second probe
    assert urls == ["https://api.deepseek.com/v1/models", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models"]
    assert all("openai.com" not in u for u in urls)


def test_bench_aborts_on_fatal_provider_error(tmp_path):
    from lcc.model import BaseProvider

    class Broke(BaseProvider):
        name, model = "broke", "m"

        def chat(self, messages, **kw):
            raise ProviderError("broke/m: the account has insufficient balance (402).", fatal=True)

    from pathlib import Path

    tasks = Path(__file__).resolve().parents[1] / "benchmarks" / "tasks"
    rows, summary, _ = run_bench(tasks, "broke", provider=Broke())
    assert len(rows) == 1 and rows[0]["fatal"]
    assert "insufficient balance" in summary["aborted"]
