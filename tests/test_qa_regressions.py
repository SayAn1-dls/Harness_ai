"""Regression tests for bugs found in the 2026-09-27 QA pass. Each test failed before its fix."""

import subprocess
import tempfile
from pathlib import Path

import pytest

from lcc.agent_loop import run_tool_loop
from lcc.agents import as_list, intake_gate, run_intake, run_planner, run_reviewer
from lcc.context_engine import _is_test
from lcc.model import ScriptedProvider, tc
from lcc.orchestrator import Orchestrator, create_task
from lcc.schemas import Budget, TaskState, TaskStatus
from lcc.session import declared_test_extras, is_repo_only
from lcc.store import HarnessStore
from lcc.tools import ToolPolicy, ToolRegistry, parse_failed_ids

GOOD_FIX = [
    tc("edit_file", path="calc.py", old_str="a - b", new_str="a + b"),
    tc("edit_file", path="tests/test_calc.py", old_str="    assert add(0, 0) == 0\n",
       new_str="    assert add(0, 0) == 0\n\ndef test_add():\n    assert add(2, 3) == 5\n"),
]


def _repo(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    root = tmp_path / "r"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "tests" / "test_calc.py").write_text("from calc import add\n\ndef test_zero():\n    assert add(0, 0) == 0\n")
    for rel, text in (extra or {}).items():
        (root / rel).write_text(text)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "i"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _run(root: Path, script: dict, **budget):
    store = HarnessStore(root)
    task = create_task(store, "T", "add() subtracts", root, issue_body="add(2, 3) returns -1; it must return 5.",
                       budget_overrides=budget or None)
    provider = ScriptedProvider(script)
    return Orchestrator(store, provider).run(task), provider


def test_unrelated_pre_existing_lint_error_does_not_block(tmp_path):
    root = _repo(tmp_path, {"legacy.py": "def old():\n    return undefined_thing\n"})
    task, _ = _run(root, {"coder": [GOOD_FIX, [tc("finish", summary="x")]]})
    assert task.status == TaskStatus.HUMAN_REVIEW and task.iteration == 1, task.last_failure


def test_new_lint_error_still_fails(tmp_path):
    root = _repo(tmp_path)
    bad = [tc("edit_file", path="calc.py", old_str="    return a - b\n",
              new_str="    return a + b\n\n\ndef broken():\n    return nope_undefined\n"), GOOD_FIX[1]]
    task, _ = _run(root, {"coder": [bad, [tc("finish", summary="x")]]}, max_iterations=1)
    assert task.status == TaskStatus.STOPPED and "nope_undefined" in (task.last_failure or "")


def test_no_recovery_calls_after_the_last_iteration(tmp_path):
    root = _repo(tmp_path)
    wrong = [tc("edit_file", path="calc.py", old_str="a - b", new_str="a * b")]
    task, provider = _run(root, {"coder": [wrong, [tc("finish", summary="x")]]}, max_iterations=1)
    assert task.status == TaskStatus.STOPPED and task.stop_reason == "max_iterations"
    assert not [a for a, _ in provider.seen if a in {"recovery", "planner"}][1:]  # only the initial plan


def test_same_failure_twice_stops_before_another_diagnosis(tmp_path):
    root = _repo(tmp_path)
    wrong = [tc("edit_file", path="calc.py", old_str="a - b", new_str="a * b")]
    undo = [tc("edit_file", path="calc.py", old_str="a * b", new_str="a - b")]
    script = {"coder": [wrong, [tc("finish", summary="x")], undo, wrong, [tc("finish", summary="x")]] * 3,
              "recovery": [{"class": "CODE_BUG", "action": "patch"}] * 5}
    task, provider = _run(root, script, max_iterations=5)
    assert task.stop_reason == "same_failure_repeated"
    assert sum(1 for a, _ in provider.seen if a == "recovery") == 1


def test_recovery_sees_the_real_failure_reason(tmp_path):
    root = _repo(tmp_path)
    no_test = [tc("edit_file", path="calc.py", old_str="a - b", new_str="a + b")]  # fixed, but unproven
    task, provider = _run(root, {"coder": [no_test, [tc("finish", summary="x")], GOOD_FIX[1:], [tc("finish", summary="y")]],
                                 "recovery": [{"class": "TEST_BUG", "action": "patch"}]})
    recovery_prompt = next(m for a, msgs in provider.seen if a == "recovery" for m in msgs if m["role"] == "user")["content"]
    assert "Add or update a test" in recovery_prompt  # the proof note, not just a green test run
    assert task.status == TaskStatus.HUMAN_REVIEW


def test_reviewer_can_reject_a_proven_fix_only_once(tmp_path):
    root = _repo(tmp_path, {"a.py": "", "b.py": "", "c.py": "", "d.py": "", "e.py": "", "f.py": "", "g.py": ""})
    blocking = {"findings": [{"category": "CORRECTNESS", "severity": "HIGH", "confidence": 0.9,
                              "description": "I just dislike it", "evidence": ["return a + b"]}]}
    task, provider = _run(root, {"coder": [GOOD_FIX, [tc("finish", summary="x")], [tc("finish", summary="same")]],
                                 "reviewer": [blocking, blocking], "recovery": [{"class": "CODE_BUG", "action": "patch"}]})
    assert task.status == TaskStatus.HUMAN_REVIEW and task.iteration == 2


def test_finish_after_a_failed_edit_in_the_same_turn_is_ignored(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    p = ScriptedProvider({"coder": [[tc("edit_file", path="m.py", old_str="nope", new_str="y"), tc("finish", summary="done")],
                                    [tc("edit_file", path="m.py", old_str="x = 1", new_str="x = 2")],
                                    [tc("finish", summary="done")]]})
    res = run_tool_loop(p, agent="coder", system="s", prompt="p", tools=_tools(tmp_path), allowed=["edit_file"], budget=Budget())
    assert res.steps == 3 and (tmp_path / "m.py").read_text() == "x = 2\n"
    assert any("finish ignored" in str(m["content"]) for m in res.messages)


def _tools(root):
    return ToolRegistry(root, ToolPolicy({"read_repository": True, "write_repository": True, "run_tests": True}), Budget())


def test_bad_tool_arguments_and_latin1_files_do_not_crash_the_task(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "latin.py").write_bytes(b"s = '\xe9'\n")
    p = ScriptedProvider({"coder": [[tc("read_file", path="a.py", start="abc")],
                                    [tc("edit_file", path="latin.py", old_str="s =", new_str="t =")],
                                    [tc("read_file", path="a.py", start=[1])],
                                    [tc("finish", summary="x")]]})
    res = run_tool_loop(p, agent="coder", system="s", prompt="p", tools=_tools(tmp_path), allowed=["read_file", "edit_file"], budget=Budget())
    obs = [m["content"] for m in res.messages if m["role"] == "tool"]
    assert res.finished and obs[0].startswith("ERROR") and obs[2].startswith("ERROR")
    assert (tmp_path / "latin.py").read_bytes() == b"t = '\xe9'\n"  # encoding preserved


def test_string_none_from_a_model_does_not_escalate():
    root = Path(tempfile.mkdtemp())
    store = HarnessStore(root)
    store.init_layout()
    task = TaskState(task_id="T", repository="r", workspace=str(root), objective="x", issue_body="y")
    r = run_intake(task, ScriptedProvider({"intake": [{"problem": "p", "intent": "i", "requirements": "one",
                                                       "acceptance_criteria": "text", "blocking_ambiguities": "none"}]}), store)
    assert r.blocking_ambiguities == [] and r.requirements == ["one"] and intake_gate(r) != "escalate"
    assert run_planner(task, ScriptedProvider({"planner": [{"steps": [{"order": "first", "files": "a.py"}],
                                                            "allowed_files": "a.py"}]}), "", store).allowed_files == ["a.py"]
    f = run_reviewer(task, ScriptedProvider({"reviewer": [{"findings": [{"confidence": "high", "evidence": "q"}]}]}), "d", store)
    assert f[0].confidence == 0.85 and f[0].evidence == ["q"]
    assert as_list("N/A") == [] and as_list(None) == [] and as_list(["a", "none"]) == ["a"]


@pytest.mark.parametrize("path,expected", [
    ("src/sum.test.js", True), ("src/App.spec.tsx", True), ("lib/__tests__/a.js", True), ("spec/user_spec.rb", True),
    ("pkg/util_test.go", True), ("tests/conftest.py", True), ("src/testament.py", False), ("contest.py", False),
])
def test_test_file_detection(path, expected):
    assert _is_test(path) is expected


def test_only_declared_test_extras_are_installed(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n[project.optional-dependencies]\ndev=["a"]\ndocs=["b"]\n'
                                             '[dependency-groups]\ntest=["c"]\n')
    assert declared_test_extras(tmp_path) == (["dev"], ["test"])
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n')
    (tmp_path / "setup.cfg").write_text("[options.extras_require]\ntesting = pytest\n")
    assert declared_test_extras(tmp_path) == (["testing"], [])


def test_failed_ids_with_spaces_and_issue_files_with_slashes(tmp_path):
    assert parse_failed_ids("FAILED tests/t.py::test_x[a b] - AssertionError") == {"tests/t.py::test_x[a b]"}
    issue = tmp_path / "issues" / "bug"
    issue.parent.mkdir()
    issue.write_text("Crash\n")
    import os

    cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        assert not is_repo_only("issues/bug")
    finally:
        os.chdir(cwd)


def test_target_code_cannot_read_credentials_or_change_git_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_API_KEY", "sk-secret-value")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    root = tmp_path / "r"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n")
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "i"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    t = ToolRegistry(root, ToolPolicy({"read_repository": True, "shell": True}), Budget())
    assert t.shell('echo "[$AI_API_KEY][$GITHUB_TOKEN]"')["stdout"].strip() == "[][]"
    sneaky = t.shell("""python3 -c "import subprocess; print(subprocess.run(['git','reset','--hard']).returncode)" """)
    assert sneaky["stdout"].strip() == "1" and "blocked by the LCC harness" in sneaky["stderr"]
    assert t.shell("git log --oneline -1")["returncode"] == 0
    other = tmp_path / "other"
    ok = t.shell(f"""python3 -c "import subprocess; print(subprocess.run(['git','init','-q','{other}']).returncode)" """)
    assert ok["stdout"].strip() == "0"  # git elsewhere (a test suite's temp repos) still works


def _docker_ok() -> bool:
    import shutil as _sh

    return bool(_sh.which("docker")) and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_ok(), reason="Docker is not running")
def test_docker_sandbox_isolates_target_code(tmp_path, monkeypatch):
    from rich.console import Console

    from lcc.config import load_config
    from lcc.model import MockProvider
    from lcc.session import parse_issue, solve

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AI_API_KEY", "sk-secret-value")
    monkeypatch.setenv("LCC_SANDBOX", "none")  # registers a restore even though solve() sets it directly
    monkeypatch.setenv("LCC_SANDBOX_VOLUME", "lcc-venv-test")
    root = tmp_path / "repo"
    shutil_copy = __import__("shutil").copytree
    shutil_copy(Path(__file__).parent / "fixtures" / "mini_repo", root)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "i"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    cfg = load_config()
    cfg.run.sandbox = "docker"
    cfg.run.outputs_dir = str(tmp_path / "out")
    summary = solve(parse_issue("Fix add so it returns the sum\n\nadd(2, 3) should be 5"), root, cfg, MockProvider(),
                    Console(quiet=True))
    assert summary["resolved"] is True  # the whole pipeline works with every test run inside the container
    import os as _os

    monkeypatch.setenv("LCC_SANDBOX", "docker")  # solve() restores the env when it returns; probe the same sandbox
    monkeypatch.setenv("LCC_SANDBOX_VOLUME", "lcc-venv-repo")
    t = ToolRegistry(root, ToolPolicy({"read_repository": True, "shell": True}), Budget())
    probe = t.shell("python -c \"import os, socket; print(repr(os.environ.get('AI_API_KEY')));"
                    " print(os.path.exists(os.path.expanduser('~/.ssh')) or os.path.exists('" + _os.path.expanduser("~") + "'));"
                    " socket.create_connection(('1.1.1.1', 53), 2)\"")
    lines = probe["stdout"].split()
    assert lines[:2] == ["None", "False"]  # no key, no home directory
    assert probe["returncode"] != 0 and ("unreachable" in probe["stderr"] or "OSError" in probe["stderr"])
