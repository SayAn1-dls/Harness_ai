"""Whole-repository audit coverage, measured speed proof for optimizations, and the full issue report."""

import subprocess
from pathlib import Path

from lcc.discover import audit_plan
from lcc.model import ScriptedProvider, tc
from lcc.orchestrator import Orchestrator, create_task
from lcc.schemas import TaskStatus
from lcc.store import HarnessStore

SLOW = ("def has_duplicates(xs):\n    for i in range(len(xs)):\n        for j in range(i + 1, len(xs)):\n"
        "            if xs[i] == xs[j]:\n                return True\n    return False\n")
FAST = "def has_duplicates(xs):\n    return len(set(xs)) != len(xs)\n"
BENCH = "from dups import has_duplicates\n\nDATA = list(range(3000))\n\n\ndef bench():\n    has_duplicates(DATA)\n"


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    (root / "tests").mkdir(parents=True)
    (root / "dups.py").write_text(SLOW)
    (root / "tests" / "test_dups.py").write_text(
        "from dups import has_duplicates\n\n\ndef test_dups():\n    assert has_duplicates([1, 2, 1])\n"
        "    assert not has_duplicates([1, 2, 3])\n    assert not has_duplicates([])\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "i"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _optimize(root: Path, new_code: str):
    store = HarnessStore(root)
    task = create_task(store, "OPT-1", "has_duplicates is quadratic", root, kind="optimize",
                       issue_body="has_duplicates compares every pair: O(n^2) on large lists.")
    script = {"coder": [[tc("write_file", path=".lcc/bench.py", content=BENCH),
                         tc("write_file", path="dups.py", content=new_code)], [tc("finish", summary="use a set")]],
              "recovery": [{"class": "CODE_BUG", "action": "escalate"}]}
    return Orchestrator(store, ScriptedProvider(script)).run(task)


def test_real_optimization_is_measured_and_accepted(tmp_path):
    root = _repo(tmp_path)
    task = _optimize(root, FAST)
    assert task.status == TaskStatus.HUMAN_REVIEW, task.last_failure
    speed = task.verification["speed"]
    assert speed["ok"] and speed["speedup"] > 5 and "faster" in speed["message"]
    assert task.verification["proof"]["kind"] == "behavior_preserved" and task.verification["proof"]["speed"]["ok"]
    committed = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=root, text=True, capture_output=True).stdout
    assert committed.split() == ["dups.py"]  # the benchmark never lands in the fix


def test_fake_optimization_is_rejected(tmp_path):
    root = _repo(tmp_path)
    same_speed = SLOW.replace("def has_duplicates(xs):", "def has_duplicates(xs):  # 'optimized'")
    task = _optimize(root, same_speed)
    assert task.status != TaskStatus.HUMAN_REVIEW
    assert "no measurable speed-up" in (task.last_failure or "")


def test_whole_repository_audit_coverage(tmp_path):
    for i in range(6):
        (tmp_path / f"m{i}.py").write_text("".join(f"def f{i}_{j}(x):\n    return x + {j}\n" for j in range(40)))
    (tmp_path / "big.py").write_text("".join(f"v{j} = {j}\n" for j in range(3000)))  # larger than one chunk
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_m.py").write_text("def test_x():\n    pass\n")
    chunks, cov = audit_plan(tmp_path, chunk_chars=8000, budget_chars=10**7)
    assert cov["files_read"] == cov["source_files"] == 7 and cov["lines_read"] == cov["source_lines"]
    assert sum(1 for c in chunks for rel, _ in c if rel == "big.py") > 1  # split, not skipped
    _, small = audit_plan(tmp_path, chunk_chars=8000, budget_chars=9000)
    assert small["files_read"] < small["source_files"] and small["unread"]  # a budget cut is reported
