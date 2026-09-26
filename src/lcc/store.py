from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable

from lcc.constants import (
    AGENT_STATE_FILE,
    ARTIFACTS_DIR,
    CACHE_DIR,
    CONTEXT_STATE_FILE,
    DOCS_DIR,
    EVENTS_FILE,
    HARNESS_DIR,
    RESEARCH_DIR,
    STATE_DIR,
    TASK_STATE_FILE,
    VERIFICATION_STATE_FILE,
)
from lcc.schemas import Event, TaskState, utcnow


class HarnessStore:
    """Filesystem source of truth. Orchestrator writes state; agents write artifacts."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.harness = self.root / HARNESS_DIR
        self.state_dir = self.harness / STATE_DIR
        self.artifacts = self.harness / ARTIFACTS_DIR
        self.docs = self.harness / DOCS_DIR
        self.research = self.harness / RESEARCH_DIR
        self.cache = self.harness / CACHE_DIR
        self.on_event: Callable[[Event], None] | None = None

    def init_layout(self) -> None:
        for d in (self.state_dir, self.artifacts, self.docs, self.research, self.cache):
            d.mkdir(parents=True, exist_ok=True)
        events = self.events_path
        if not events.exists():
            events.write_text("", encoding="utf-8")

    @property
    def task_path(self) -> Path:
        return self.state_dir / TASK_STATE_FILE

    @property
    def context_path(self) -> Path:
        return self.state_dir / CONTEXT_STATE_FILE

    @property
    def agent_path(self) -> Path:
        return self.state_dir / AGENT_STATE_FILE

    @property
    def verification_path(self) -> Path:
        return self.state_dir / VERIFICATION_STATE_FILE

    @property
    def events_path(self) -> Path:
        return self.state_dir / EVENTS_FILE

    def load_task(self) -> TaskState | None:
        if not self.task_path.exists():
            return None
        return TaskState.model_validate_json(self.task_path.read_text(encoding="utf-8"))

    def save_task(self, task: TaskState) -> None:
        task.touch()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.task_path.with_suffix(".json.tmp")
        tmp.write_text(task.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(self.task_path)

    def append_event(self, event: Event) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        line = event.model_dump_json() + "\n"
        with self.events_path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # a display hook must never break the run
                pass

    def events(self) -> list[Event]:
        if not self.events_path.exists():
            return []
        out: list[Event] = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(Event.model_validate_json(line))
        return out

    def write_json(self, rel: str, payload: Any) -> Path:
        path = self.artifacts / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if hasattr(payload, "model_dump"):
            text = json.dumps(payload.model_dump(mode="json"), indent=2)
        else:
            text = json.dumps(payload, indent=2, default=str)
        path.write_text(text, encoding="utf-8")
        return path

    def read_json(self, rel: str) -> Any:
        path = self.artifacts / rel
        return json.loads(path.read_text(encoding="utf-8"))

    def write_text(self, rel: str, text: str) -> Path:
        path = self.artifacts / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def save_sidecar(self, name: str, payload: dict[str, Any]) -> None:
        mapping = {
            "context": self.context_path,
            "agent": self.agent_path,
            "verification": self.verification_path,
        }
        path = mapping[name]
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def emit(self, task: TaskState, event: str, **kwargs: Any) -> None:
        self.append_event(
            Event(
                event=event,
                task=task.task_id,
                agent=kwargs.get("agent", task.current_agent),
                result=kwargs.get("result"),
                status=task.status.value,
                artifacts=list(kwargs.get("artifacts") or []),
                data={k: v for k, v in kwargs.items() if k not in {"agent", "result", "artifacts"}},
            )
        )


def write_handoff(store: HarnessStore, task: TaskState, next_steps: Iterable[str]) -> Path:
    next_list = "\n".join(f"{i}. {s}" for i, s in enumerate(next_steps, 1)) or "1. Await human direction"
    completed = [e.event for e in store.events() if e.event.endswith("_COMPLETED") or e.event == "AGENT_COMPLETED"]
    failures = [e for e in store.events() if e.result in {"fail", "failure", "error"} or (e.event or "").endswith("_FAILED")]
    fail_txt = "\n".join(f"- {e.event}: {e.data}" for e in failures[-8:]) or "- none"
    decisions = [e for e in store.events() if e.event == "DECISION"]
    dec_txt = "\n".join(f"- {e.data.get('decision', e.data)}" for e in decisions[-12:]) or "- none recorded"
    text = f"""# Current Handoff

Task:
{task.task_id} — {task.objective}

Status: `{task.status.value}`
Lane: `{task.lane.value}`
Iteration: {task.iteration}
Context snapshot: {task.context_snapshot or "none"}
Branch: {task.branch or "none"}
Score: {task.global_score}

## 1. What are we building?

{task.objective}

## 2. What has been completed?

{chr(10).join(f"- {c}" for c in completed[-20:]) or "- intake not finished"}

## 3. What is currently being worked on?

Agent: {task.current_agent or "orchestrator"}
Status: {task.status.value}

## 4. What decisions have already been made?

{dec_txt}

## 5. What failed and why?

{fail_txt}

Last failure: {task.last_failure or "none"}
Repeated failure count: {task.repeated_failure_count}

## 6. What should the next agent do next?

{next_list}

## Budget

tokens: {task.budget.tokens_used}/{task.budget.tokens} (in {task.budget.tokens_in}, out {task.budget.tokens_out}, cached {task.budget.tokens_cached})
model calls: {task.budget.model_calls}, tool calls: {task.budget.tool_calls}
runtime: {task.budget.runtime_used_seconds}s
iterations: {task.iteration}/{task.budget.max_iterations}
stop_reason: {task.stop_reason or "n/a"}

Generated at: {utcnow().isoformat()}
"""
    # Per-task handoff lives with the task's artifacts so a run never clobbers the project's own docs/HANDOFF.md.
    path = store.artifacts / "HANDOFF.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
