"""Proof-carrying fixes (lcc verify, manual steps) and the test-strength (mutation) check."""

import json
import shutil
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from lcc.cli import app
from lcc.model import ScriptedProvider, tc
from lcc.orchestrator import Orchestrator, create_task
from lcc.proof import manual_steps, mutation_check, verify
from lcc.schemas import TaskStatus
from lcc.store import HarnessStore

BUGGY = "def clamp(x, lo, hi):\n    return x\n"
FIXED_OLD, FIXED_NEW = "    return x\n", "    if x < lo:\n        return lo\n    if x > hi:\n        return hi\n    return x\n"
WEAK = "from clamp import clamp\n\n\ndef test_clamp():\n    assert clamp(5, 0, 3) != 5\n"
STRONG = ("from clamp import clamp\n\n\ndef test_clamp():\n    assert clamp(-1, 0, 3) == 0\n    assert clamp(5, 0, 3) == 3\n"
          "    assert clamp(2, 0, 3) == 2\n")


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "r"
    (root / "tests").mkdir(parents=True)
    (root / "clamp.py").write_text(BUGGY)
    (root / "tests" / "test_existing.py").write_text("from clamp import clamp\n\n\ndef test_inside():\n    assert clamp(1, 0, 3) == 1\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "i"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


def _fix(test_body: str):
    return [tc("edit_file", path="clamp.py", old_str=FIXED_OLD, new_str=FIXED_NEW),
            tc("write_file", path="tests/test_clamp.py", content=test_body)]


def _run(root, script, **env):
    store = HarnessStore(root)
    task = create_task(store, "T-1", "clamp() does not clamp", root,
                       issue_body="clamp(5, 0, 3) returns 5; it must stay within [lo, hi].")
    return Orchestrator(store, ScriptedProvider(script)).run(task), store


def test_verified_fix_carries_a_proof_that_replays(tmp_path):
    root = _repo(tmp_path)
    task, store = _run(root, {"coder": [_fix(STRONG), [tc("finish", summary="clamp")]]})
    assert task.status == TaskStatus.HUMAN_REVIEW, task.last_failure
    proof = json.loads((store.artifacts / "proof.json").read_text())
    assert proof["kind"] == "new_tests" and proof["targets"] == ["tests/test_clamp.py"] and proof["level"] == 5
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True).stdout.strip()
    assert proof["head"] == head and proof["base"] == task.base_commit
    res = verify(root, proof["base"], proof["head"], proof["targets"])
    assert res["valid"] and res["fails_on_base"] and res["passes_on_head"]
    assert subprocess.run(["git", "status", "--porcelain"], cwd=root, text=True, capture_output=True).stdout.strip() in {"", "?? harness/"}
    # the CLI tells the same story, with the right exit code
    pfile = tmp_path / "p.json"
    pfile.write_text(json.dumps(proof))
    out = CliRunner().invoke(app, ["verify", "--repo", str(root), "--proof", str(pfile)])
    assert out.exit_code == 0 and "PROOF HOLDS" in out.output


def test_manual_steps_work_without_the_harness(tmp_path):
    root = _repo(tmp_path)
    task, store = _run(root, {"coder": [_fix(STRONG), [tc("finish", summary="clamp")]]})
    proof = json.loads((store.artifacts / "proof.json").read_text())
    copy = tmp_path / "maintainer"
    shutil.copytree(root, copy)
    lines = [ln.split("#")[0].strip() for ln in manual_steps(proof).splitlines()]
    results = []
    for cmd in lines:
        p = subprocess.run(cmd.replace("python -m pytest", f"{__import__('sys').executable} -m pytest -q -p no:cacheprovider"), shell=True,
                           cwd=copy, capture_output=True, text=True, env={"PATH": __import__("os").environ["PATH"], "PYTHONPATH": str(copy)})
        if "pytest" in cmd:
            results.append(p.returncode)
    assert results[0] != 0 and results[1] == 0  # fails on the original code, passes with the fix


def test_a_proof_that_does_not_hold_is_rejected(tmp_path):
    root = _repo(tmp_path)
    (root / "tests" / "test_trivial.py").write_text("def test_nothing():\n    assert True\n")
    subprocess.run(["git", "add", "-A"], cwd=root)
    subprocess.run(["git", "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "claims a fix"], cwd=root)
    res = verify(root, "HEAD~1", "HEAD")
    assert not res["valid"] and not res["fails_on_base"]


def test_test_strength_separates_weak_from_strong_tests(tmp_path):
    for body, expect in ((WEAK, 0), (STRONG, 2)):
        root = _repo(tmp_path / str(expect))
        (root / "clamp.py").write_text(BUGGY.replace(FIXED_OLD, FIXED_NEW))
        (root / "tests" / "test_clamp.py").write_text(body)
        res = mutation_check(root, ["tests/test_clamp.py"], ["clamp.py"])
        assert res["total"] == 4 and res["killed"] >= expect
        if expect == 0:
            assert res["killed"] == 0 and len(res["survived"]) == 4  # the weak test is decoration
    assert (root / "clamp.py").read_text() == BUGGY.replace(FIXED_OLD, FIXED_NEW)  # always restored


def test_gate_mode_sends_a_weak_test_back_once(tmp_path, monkeypatch):
    monkeypatch.setenv("LCC_MUTATION", "gate")
    root = _repo(tmp_path)
    script = {"coder": [_fix(WEAK), [tc("finish", summary="clamp")],
                        [tc("write_file", path="tests/test_clamp.py", content=STRONG)], [tc("finish", summary="stronger")]],
              "recovery": [{"class": "TEST_BUG", "action": "patch", "guidance": "strengthen the test"}]}
    task, store = _run(root, script)
    assert task.status == TaskStatus.HUMAN_REVIEW and task.iteration == 2, task.last_failure
    first = json.loads((store.artifacts / "verification_1.json").read_text())
    assert any("Weak test" in e for e in first["evidence"])
    assert task.verification["mutation"]["killed"] >= 2


def test_mutations_never_touch_strings_or_comments():
    from lcc.proof import mutants

    got = mutants([("m.py", 1, "    if n < 0:"), ("m.py", 2, "        raise ValueError('n must be at least 0')"),
                   ("m.py", 3, "    label = 'a == b'  # 1 + 1")])
    assert [m["op"] for m in got] == ["< → <=", "raise → pass"]  # the string and comment on line 3 are left alone
