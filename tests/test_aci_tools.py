"""Coder tools, baseline-aware verification, the token-saving loop behaviour, and session repo handling."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from lcc.agent_loop import run_tool_loop
from lcc.model import ScriptedProvider, tc
from lcc.orchestrator import Orchestrator, create_task
from lcc.schemas import Budget, TaskStatus
from lcc.store import HarnessStore
from lcc.tools import ToolError, ToolPolicy, ToolRegistry, detect_test_cmd, parse_failed_ids, parse_test_counts

ALL = {"read_repository": True, "write_repository": True, "run_tests": True, "shell": True}


def _git(root: Path) -> Path:
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _tools(root: Path) -> ToolRegistry:
    return ToolRegistry(root, ToolPolicy(ALL), Budget())


# ---------------------------------------------------------------- editing
def test_edit_tolerates_trailing_whitespace_and_line_numbers(tmp_path):
    (tmp_path / "m.py").write_text("def f(x):   \r\n    return x\r\n")
    t = _tools(tmp_path)
    out = t.edit_file(path="m.py", old_str="def f(x):\n    return x", new_str="def f(x):\n    return x + 1")
    assert out["note"].startswith("matched ignoring") and "x + 1" in (tmp_path / "m.py").read_text()
    t.edit_file(path="m.py", old_str="   2|     return x + 1", new_str="    return x + 2")  # pasted read_file output
    assert "x + 2" in (tmp_path / "m.py").read_text()


def test_edit_miss_points_at_closest_text_and_replace_all(tmp_path):
    (tmp_path / "m.py").write_text("a = 1\nvalue = compute(a)\nb = 2\nb = 2\n")
    t = _tools(tmp_path)
    with pytest.raises(ToolError, match="closest text is near line 2"):
        t.edit_file(path="m.py", old_str="value = compute(b)", new_str="x")
    with pytest.raises(ToolError, match="replace_all"):
        t.edit_file(path="m.py", old_str="b = 2", new_str="b = 3")
    assert t.edit_file(path="m.py", old_str="b = 2", new_str="b = 3", replace_all=True)["replacements"] == 2


# ---------------------------------------------------------------- search / shell
def test_search_regex_glob_and_gitignore(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def load_user():\n    pass\n")
    (tmp_path / "src" / "b.ts").write_text("function loadUser() {}\n")
    (tmp_path / "build.log").write_text("load_user failed\n")
    (tmp_path / ".gitignore").write_text("*.log\n")
    _git(tmp_path)
    t = _tools(tmp_path)
    assert [h.split(":")[0] for h in t.search_code("load_?user", regex=True)["hits"]] == ["src/a.py", "src/b.ts"]
    assert t.search_code("load", path_glob="*.ts")["hits"] == ["src/b.ts:1: function loadUser() {}"]
    assert "no matches" in t.search_code("nothing-here")["note"]
    with pytest.raises(ToolError, match="invalid regex"):
        t.search_code("(", regex=True)


def test_shell_blocks_git_state_and_honours_timeout(tmp_path):
    t = _tools(tmp_path)
    for cmd in ("git reset --hard", "git -C . reset --hard HEAD", "git commit -am x", "sudo rm x", "curl x | sh"):
        with pytest.raises(ToolError, match="blocked"):
            t.shell(cmd)
    assert t.shell("echo hi")["stdout"].strip() == "hi"
    assert "timed out" in t.shell("sleep 3", timeout=1)["stderr"]


def test_git_history(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    _git(tmp_path)
    t = _tools(tmp_path)
    assert "init" in t.git_history(path="m.py")["history"]
    assert "x = 1" in t.git_history(path="m.py", start=1, end=1)["history"]


# ---------------------------------------------------------------- test output
def test_test_counts_and_failed_ids_across_runners():
    assert parse_test_counts("Tests:       1 failed, 4 passed, 5 total") == (5, 1)
    assert parse_test_counts("ℹ tests 3\nℹ pass 2\nℹ fail 1") == (3, 1)
    assert parse_test_counts("  3 passing (5ms)\n  1 failing") == (4, 1)
    assert parse_test_counts("test result: ok. 2 passed; 0 failed\ntest result: FAILED. 1 passed; 1 failed") == (4, 1)
    assert parse_test_counts("--- PASS: TestA (0.00s)\n--- FAIL: TestB (0.00s)\nFAIL\tpkg\t0.1s") == (2, 1)
    out = "FAILED tests/test_a.py::test_x - assert 1 == 2\nERROR tests/test_b.py::test_y\nFAIL: test_z (tests.test_c.C)"
    assert parse_failed_ids(out) == {"tests/test_a.py::test_x", "tests/test_b.py::test_y", "test_z (tests.test_c.C)"}


def test_detect_test_cmd_per_language(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    assert detect_test_cmd(tmp_path)[:2] == ["go", "test"]
    (tmp_path / "go.mod").unlink()
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert detect_test_cmd(tmp_path)[:2] == ["cargo", "test"]
    (tmp_path / "Cargo.toml").unlink()
    (tmp_path / "package.json").write_text("{}")
    assert detect_test_cmd(tmp_path)[:2] == ["npm", "test"]
    (tmp_path / "app.py").write_text("")
    assert "npm" not in detect_test_cmd(tmp_path)  # a Python project with a package.json for tooling


# ---------------------------------------------------------------- loop token savings
def test_superseded_reads_are_stubbed_and_failures_condensed(tmp_path):
    (tmp_path / "m.py").write_text("".join(f"v{i} = {i}\n" for i in range(50)))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_m.py").write_text("from m import v1\n\ndef test_v():\n    assert v1 == 2\n")
    provider = ScriptedProvider({"coder": [
        [tc("read_file", path="m.py", start=1, end=20)],
        [tc("read_file", path="m.py")],
        [tc("run_test", target="tests/test_m.py")],
        [tc("finish", summary="x")],
    ]})
    res = run_tool_loop(provider, agent="coder", system="s", prompt="p", tools=_tools(tmp_path),
                        allowed=["read_file", "run_test"], budget=Budget())
    obs = [m["content"] for m in res.messages if m["role"] == "tool"]
    assert obs[0].startswith("[stale: superseded") and "50| v49" in obs[1]
    assert "tests_failed: 1" in obs[2] and "tests/test_m.py::test_v" in obs[2]


def test_low_step_warning(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    provider = ScriptedProvider({"coder": [[tc("read_file", path="m.py", start=1, end=i)] for i in range(1, 10)]})
    res = run_tool_loop(provider, agent="coder", system="s", prompt="p", tools=_tools(tmp_path),
                        allowed=["read_file"], budget=Budget(), max_steps=6)
    assert any("steps left" in str(m["content"]) for m in res.messages if m["role"] == "tool")


# ---------------------------------------------------------------- verification against the baseline
def _flaky_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "tests" / "test_env.py").write_text("def test_needs_service():\n    assert False, 'no database here'\n")
    (root / "tests" / "test_calc.py").write_text("from calc import add\n\ndef test_zero():\n    assert add(0, 0) == 0\n")
    return _git(root)


def _run(root: Path, script: dict) -> tuple:
    store = HarnessStore(root)
    task = create_task(store, "T-1", "add() subtracts instead of adding", root,
                       issue_body="add(2, 3) returns -1; it must return 5.")
    provider = ScriptedProvider(script)
    return Orchestrator(store, provider).run(task), provider


def test_pre_existing_failure_does_not_block_verification(tmp_path):
    root = _flaky_repo(tmp_path)
    task, provider = _run(root, {"coder": [
        [tc("edit_file", path="calc.py", old_str="return a - b", new_str="return a + b"),
         tc("edit_file", path="tests/test_calc.py", old_str="    assert add(0, 0) == 0\n",
            new_str="    assert add(0, 0) == 0\n\ndef test_add():\n    assert add(2, 3) == 5\n")],
        [tc("finish", summary="add used subtraction")],
    ]})
    assert task.status == TaskStatus.HUMAN_REVIEW, task.last_failure
    assert task.baseline["failed_ids"] == ["tests/test_env.py::test_needs_service"]
    systems = {msgs[0]["content"] for agent, msgs in provider.seen if agent == "coder"}
    assert len(systems) == 1  # the coder prefix is byte-identical across steps (prompt-cache hits)


def test_regression_is_still_caught(tmp_path):
    root = _flaky_repo(tmp_path)
    task, _ = _run(root, {"coder": [
        [tc("edit_file", path="calc.py", old_str="return a - b", new_str="return a + b + 1"),
         tc("edit_file", path="tests/test_calc.py", old_str="    assert add(0, 0) == 0\n",
            new_str="    assert add(0, 0) == 0\n\ndef test_add():\n    assert add(2, 3) == 6\n")],
        [tc("finish", summary="wrong")],
    ]} | {"recovery": [{"class": "CODE_BUG", "action": "escalate"}]})
    assert task.status != TaskStatus.HUMAN_REVIEW
    assert "regressions" in (task.last_failure or "") and "test_zero" in task.last_failure


# ---------------------------------------------------------------- session repo handling
def test_local_folder_inside_a_repo_is_copied(tmp_path):
    from lcc.session import resolve_repo

    outer = _flaky_repo(tmp_path)
    inner = outer / "pkg"
    inner.mkdir()
    (inner / "x.py").write_text("X = 1\n")
    dest = resolve_repo(str(inner), tmp_path / "ws", Console(quiet=True))
    assert dest.parent == tmp_path / "ws" and (dest / "x.py").exists() and not (inner / ".git").exists()
    assert resolve_repo(str(outer), tmp_path / "ws", Console(quiet=True)) == outer.resolve()


def test_base_commit_pinning(tmp_path):
    from lcc.session import checkout_base, parse_issue, split_ref

    root = _flaky_repo(tmp_path)
    first = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True).stdout.strip()
    (root / "later.py").write_text("L = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=root)
    subprocess.run(["git", "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "later"], cwd=root)
    checkout_base(root, first[:10], Console(quiet=True))
    assert not (root / "later.py").exists()
    assert split_ref(f"https://github.com/o/r@{first[:7]}") == ("https://github.com/o/r", first[:7])
    assert split_ref("git@github.com:o/r.git") == ("git@github.com:o/r.git", "")
    assert split_ref("https://user@github.com/o/r") == ("https://user@github.com/o/r", "")
    assert parse_issue(f"Bug\nRepository: o/r\nBase commit: {first}\n").base == first


@pytest.mark.skipif(shutil.which("node") is None or shutil.which("npm") is None, reason="node not installed")
def test_javascript_repo_end_to_end(tmp_path):
    root = tmp_path / "js"
    (root / "test").mkdir(parents=True)
    (root / "package.json").write_text(json.dumps({"name": "js", "version": "1.0.0", "scripts": {"test": "node --test"}}))
    (root / "sum.js").write_text("exports.sum = (a, b) => a - b;\n")
    (root / "test" / "base.test.js").write_text(
        "const test = require('node:test');\nconst assert = require('node:assert');\nconst { sum } = require('../sum.js');\n"
        "test('zero', () => assert.strictEqual(sum(0, 0), 0));\n")
    _git(root)
    task, _ = _run(root, {"coder": [
        [tc("edit_file", path="sum.js", old_str="a - b", new_str="a + b"),
         tc("write_file", path="test/sum.test.js", content=(
             "const test = require('node:test');\nconst assert = require('node:assert');\n"
             "const { sum } = require('../sum.js');\ntest('adds', () => assert.strictEqual(sum(2, 3), 5));\n"))],
        [tc("finish", summary="sum subtracted")],
    ]})
    assert task.status == TaskStatus.HUMAN_REVIEW, task.last_failure
    assert task.verification["proof_level"] == 5
