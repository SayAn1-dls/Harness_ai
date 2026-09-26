import shutil
from pathlib import Path

import pytest

from lcc.agent_loop import run_tool_loop
from lcc.metering import MeteredProvider
from lcc.model import MockProvider, ProviderError, ScriptedProvider, get_provider, tc
from lcc.orchestrator import Orchestrator, create_task
from lcc.schemas import Budget, TaskState, TaskStatus
from lcc.store import HarnessStore
from lcc.tools import ToolError, ToolPolicy, ToolRegistry, parse_test_counts

FIXTURE = Path(__file__).parent / "fixtures" / "mini_repo"
WRITE = {"read_repository": True, "write_repository": True, "run_tests": True}


def _repo(tmp_path: Path) -> Path:
    dest = tmp_path / "repo"
    shutil.copytree(FIXTURE, dest)
    return dest


def _tools(root: Path, budget: Budget | None = None, **policy) -> ToolRegistry:
    return ToolRegistry(root, ToolPolicy(WRITE, **policy), budget or Budget())


# ---------------------------------------------------------------- tool loop
def test_tool_loop_reads_edits_and_finishes(tmp_path):
    root = _repo(tmp_path)
    provider = ScriptedProvider({"coder": [
        [tc("read_file", path="app.py")],
        [tc("edit_file", path="app.py", old_str="return a - b", new_str="return a + b")],
        [tc("finish", summary="fixed add")],
    ]})
    tools = _tools(root)
    res = run_tool_loop(provider, agent="coder", system="s", prompt="p", tools=tools, allowed=["read_file", "edit_file"], budget=Budget())
    assert res.finished and res.stop_reason == "finished" and res.summary == "fixed add"
    assert tools.changed == ["app.py"]
    assert "return a + b" in (root / "app.py").read_text()
    tool_msgs = [m for m in res.messages if m["role"] == "tool"]
    assert "1| def add" in tool_msgs[0]["content"]


def test_tool_loop_repeat_guard(tmp_path):
    root = _repo(tmp_path)
    same = [tc("read_file", path="app.py")]
    provider = ScriptedProvider({"coder": [same, same, same, [tc("finish", summary="x")]]})
    res = run_tool_loop(provider, agent="coder", system="s", prompt="p", tools=_tools(root), allowed=["read_file"], budget=Budget())
    assert res.stop_reason == "repeated_tool_call"
    assert "WARNING" in [m for m in res.messages if m["role"] == "tool"][1]["content"]


def test_tool_loop_disallowed_tool_and_budget(tmp_path):
    root = _repo(tmp_path)
    provider = ScriptedProvider({"coder": [[tc("shell", command="ls")], [tc("finish", summary="x")]]})
    res = run_tool_loop(provider, agent="coder", system="s", prompt="p", tools=_tools(root), allowed=["read_file"], budget=Budget())
    assert "not available" in [m for m in res.messages if m["role"] == "tool"][0]["content"]
    spent = Budget(tokens=10, tokens_used=10)
    res = run_tool_loop(provider, agent="coder", system="s", prompt="p", tools=_tools(root), allowed=[], budget=spent)
    assert res.stop_reason == "budget_exceeded" and not res.finished


# ---------------------------------------------------------------- tools
def test_edit_file_rules(tmp_path):
    root = _repo(tmp_path)
    (root / "dup.py").write_text("x = 1\nx = 1\n")
    tools = _tools(root)
    with pytest.raises(ToolError, match="not found"):
        tools.edit_file(path="app.py", old_str="nope", new_str="y")
    with pytest.raises(ToolError, match="2 times"):
        tools.edit_file(path="dup.py", old_str="x = 1", new_str="x = 2")
    with pytest.raises(ToolError, match="would not parse"):
        tools.edit_file(path="app.py", old_str="return a - b", new_str="return a +")
    assert "return a - b" in (root / "app.py").read_text()


def test_write_scope_policy(tmp_path):
    root = _repo(tmp_path)
    tools = _tools(root, allowed_paths=["app.py"])
    with pytest.raises(ToolError, match="forbidden"):
        tools.write_file(path="harness/state/task_state.json", content="{}")
    tools.write_file(path="extra.py", content="X = 1\n")
    assert tools.scope_expansions == ["extra.py"]
    strict = _tools(root, allowed_paths=["app.py"], scope_mode="strict")
    with pytest.raises(ToolError, match="outside the plan"):
        strict.write_file(path="other.py", content="X = 1\n")


def test_parse_test_counts():
    assert parse_test_counts("....\nRan 4 tests in 0.01s\n\nOK") == (4, 0)
    assert parse_test_counts("Ran 3 tests in 0.1s\nFAILED (failures=1, errors=1)") == (3, 2)
    assert parse_test_counts("1 failed, 2 passed in 0.03s") == (3, 1)
    assert parse_test_counts("no tests ran in 0.01s") == (0, 0)


def test_zero_tests_is_not_a_pass(tmp_path):
    (tmp_path / "lib.py").write_text("X = 1\n")
    res = _tools(tmp_path).run_test()
    assert res["tests_run"] == 0 and res["ok"] is False


# ---------------------------------------------------------------- providers
def test_metering_charges_budget(tmp_path):
    store = HarnessStore(tmp_path)
    store.init_layout()
    task = TaskState(task_id="T", repository="r", workspace=str(tmp_path), objective="x")
    llm = MeteredProvider(MockProvider(), store, task)
    llm.structured_output("hello", system="You are the Issue Analyst", agent="intake")
    assert task.budget.tokens_used > 0 and task.budget.model_calls == 1
    assert [e.event for e in store.events()] == ["MODEL_CALL"]


def test_real_provider_requires_key(monkeypatch):
    for k in ("LCC_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(ProviderError, match="DEEPSEEK_API_KEY"):
        get_provider("deepseek")


# ---------------------------------------------------------------- orchestrator
def _run(tmp_path, script, **budget):
    root = _repo(tmp_path)
    store = HarnessStore(root)
    task = create_task(store, "GH-1", "Fix add() so add(2,3) == 5", root,
                       issue_body="add subtracts instead of adding", budget_overrides=budget)
    provider = ScriptedProvider(script)
    return Orchestrator(store, provider).run(task), provider, store, root


def test_blocking_ambiguity_escalates_before_coding(tmp_path):
    intake = {"problem": "p", "intent": "i", "requirements": ["r"], "acceptance_criteria": ["a"], "constraints": [],
              "ambiguities": [], "blocking_ambiguities": ["Which pricing rule is wanted?"], "risk": "low"}
    task, provider, _, _ = _run(tmp_path, {"intake": [intake]})
    assert task.status == TaskStatus.ESCALATED and task.stop_reason == "blocking_ambiguity"
    assert not provider.prompts_for("coder")


def test_recovery_carries_failure_into_next_attempt(tmp_path):
    wrong = [tc("edit_file", path="app.py", old_str="return a - b", new_str="return a * b")]
    right = [tc("edit_file", path="app.py", old_str="return a * b", new_str="return a + b")]
    done = [tc("finish", summary="done")]
    task, provider, store, root = _run(tmp_path, {"coder": [wrong, done, right, done]})
    assert task.status == TaskStatus.HUMAN_REVIEW
    assert task.iteration == 2 and len(task.history) == 1
    assert task.history[0].changed_files == ["app.py"]
    second = provider.prompts_for("coder")[2]  # first call of attempt 2
    assert "attempt 2" in second.lower() and "6 != 5" in second
    assert "return a * b" in second  # the current diff is shown
    assert "return a + b" in (root / "app.py").read_text()
    assert any(e.event == "RECOVERY" for e in store.events())


def test_same_failure_twice_stops(tmp_path):
    wrong = [tc("write_file", path="app.py", content="def add(a, b):\n    return a * b\n")]
    done = [tc("finish", summary="done")]
    task, _, _, _ = _run(tmp_path, {"coder": [wrong, done] * 5})
    assert task.status == TaskStatus.STOPPED
    assert task.stop_reason == "same_failure_repeated"
    assert task.iteration == 2


def test_max_iterations_stops(tmp_path):
    attempts = []
    for i in range(3):
        attempts += [[tc("write_file", path="app.py", content=f"def add(a, b):\n    return a * b + {i}\n")], [tc("finish", summary="x")]]
    task, _, _, _ = _run(tmp_path, {"coder": attempts}, max_iterations=3)
    assert task.status == TaskStatus.STOPPED and task.stop_reason == "max_iterations"
    assert task.budget.tokens_used > 0 and task.budget.tool_calls > 0
