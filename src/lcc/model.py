from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

import httpx


class ModelProvider(Protocol):
    name: str

    def complete(self, prompt: str, *, system: str = "", max_tokens: int = 2048) -> str: ...

    def structured_output(self, prompt: str, *, system: str = "", schema_hint: str = "") -> dict[str, Any]: ...


@dataclass
class Completion:
    text: str
    tokens: int


class MockProvider:
    """Deterministic backend for tests and offline development."""

    name = "mock"

    def __init__(self, script: dict[str, Any] | None = None) -> None:
        self.script = script or {}

    def complete(self, prompt: str, *, system: str = "", max_tokens: int = 2048) -> str:
        key = self._key(system, prompt)
        if key in self.script:
            val = self.script[key]
            return val if isinstance(val, str) else json.dumps(val)
        if "intake" in system.lower() or "issue analyst" in system.lower():
            return json.dumps(
                {
                    "problem": "Implement the requested change.",
                    "intent": "Satisfy the issue with a minimal patch.",
                    "requirements": ["Meet stated acceptance criteria"],
                    "acceptance_criteria": [{"id": "AC-01", "text": "Tests pass", "satisfied": False}],
                    "constraints": ["Do not modify main directly"],
                    "ambiguities": [],
                    "risk": "low",
                }
            )
        if "planner" in system.lower():
            files = ["app.py", "tests/test_app.py"] if "add" in prompt.lower() else []
            return json.dumps(
                {
                    "steps": [
                        {
                            "order": 1,
                            "action": "Implement requested behavior",
                            "files": files,
                            "verification": "unit tests",
                        }
                    ],
                    "allowed_files": files,
                    "forbidden_files": ["harness/state/task_state.json"],
                }
            )
        if "implementation agent" in system.lower() or "implement this plan" in prompt.lower():
            if "add" in prompt.lower():
                return json.dumps(
                    {
                        "files": [{"path": "app.py", "content": "def add(a, b):\n    return a + b\n"}],
                        "summary": "Fix add() to return the sum",
                    }
                )
            return json.dumps({"files": [], "summary": "no heuristic patch"})
        if "reviewer" in system.lower() or "adversarial" in system.lower():
            return json.dumps({"findings": []})
        if "judge" in system.lower():
            return json.dumps({"decision": "PASS", "confidence": 0.8, "findings": []})
        if "recovery" in system.lower():
            return json.dumps({"class": "CODE_BUG", "action": "patch", "notes": "retry with tighter patch"})
        return json.dumps({"ok": True, "notes": "mock complete"})

    def structured_output(self, prompt: str, *, system: str = "", schema_hint: str = "") -> dict[str, Any]:
        raw = self.complete(prompt, system=system)
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {"value": data}
        except json.JSONDecodeError:
            return {"text": raw}

    def _key(self, system: str, prompt: str) -> str:
        return f"{system[:40]}|{prompt[:80]}"


class OpenAICompatibleProvider:
    """Works with OpenAI, xAI Grok, and other chat-completions gateways."""

    name = "openai_compat"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("LCC_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        self.base_url = (base_url or os.environ.get("LCC_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.model = model or os.environ.get("LCC_MODEL") or "gpt-4.1"

    def complete(self, prompt: str, *, system: str = "", max_tokens: int = 2048) -> str:
        if not self.api_key:
            return MockProvider().complete(prompt, system=system, max_tokens=max_tokens)
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system or "You are a software engineering agent."},
                {"role": "user", "content": prompt},
            ],
        }
        with httpx.Client(timeout=120) as client:
            r = client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
        return data["choices"][0]["message"]["content"]

    def structured_output(self, prompt: str, *, system: str = "", schema_hint: str = "") -> dict[str, Any]:
        sys = system + "\nReturn ONLY valid JSON. " + (schema_hint or "")
        raw = self.complete(prompt, system=sys)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]
            if raw.endswith("```"):
                raw = raw[: -3]
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {"value": data}
        except json.JSONDecodeError:
            start = raw.find("{")
            end = raw.rfind("}")
            if start >= 0 and end > start:
                return json.loads(raw[start : end + 1])
            return {"text": raw}


def get_provider() -> ModelProvider:
    kind = os.environ.get("LCC_PROVIDER", "mock").lower()
    if kind in {"openai", "openai_compat", "grok", "xai"}:
        return OpenAICompatibleProvider()
    return MockProvider()
