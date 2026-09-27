"""Repo-only mode end to end, offline: a fake GitHub (local bare repositories behind git `insteadOf`, and a
stub `gh` on PATH), a repository with two planted bugs, and a scripted model."""

import json
import os
import stat
import subprocess
from pathlib import Path

from rich.console import Console

from lcc.config import load_config
from lcc.discover import Candidate, from_static_analysis, rank
from lcc.model import ScriptedProvider, tc
from lcc.session import auto_fix, is_repo_only

GH_STUB = r"""#!/bin/sh
echo "$@" >> "$GH_LOG"
case "$1 $2" in
  "api user") echo bot ;;
  "api repos/acme/demo") printf 'false\tmain\n' ;;
  "api repos/bot/demo") exit 0 ;;
  "repo fork") exit 0 ;;
  "pr list") echo "" ;;
  "pr create") n=$(grep -c "^pr create" "$GH_LOG"); echo "https://github.com/acme/demo/pull/$n" ;;
  *) echo "unexpected gh call: $@" >&2; exit 1 ;;
esac
"""


def _sh(cwd, *args):
    subprocess.run(list(args), cwd=cwd, check=True, capture_output=True)


def _fake_github(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    src = tmp_path / "src"
    (src / "tests").mkdir(parents=True)
    (src / "stats.py").write_text("def mean(values):\n    return sum(values) / (len(values) - 1)\n")
    (src / "calc.py").write_text("def append_item(x, bucket=[]):\n    bucket.append(x)\n    return bucket\n")
    (src / "tests" / "test_basic.py").write_text("from calc import append_item\n\ndef test_append():\n    assert append_item(1) == [1]\n")
    _sh(src, "git", "init", "-q", "-b", "main")
    _sh(src, "git", "add", "-A")
    _sh(src, "git", "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "init")
    upstream, fork = tmp_path / "upstream.git", tmp_path / "fork.git"
    _sh(tmp_path, "git", "clone", "-q", "--bare", str(src), str(upstream))
    _sh(tmp_path, "git", "init", "-q", "--bare", str(fork))
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f'[url "{upstream}"]\n\tinsteadOf = https://github.com/acme/demo.git\n'
                         f'\tinsteadOf = https://github.com/acme/demo\n'
                         f'[url "{fork}"]\n\tinsteadOf = https://github.com/bot/demo.git\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(GH_STUB)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_LOG", str(tmp_path / "gh.log"))
    return upstream, fork, tmp_path / "gh.log"


def test_repo_only_input_detection(tmp_path):
    assert is_repo_only("https://github.com/acme/demo")
    assert is_repo_only("https://github.com/acme/demo.git")
    assert is_repo_only(str(tmp_path))
    assert not is_repo_only("https://github.com/acme/demo/issues/3")
    assert not is_repo_only("Fix the crash in parser\n\nwhen input is empty")
    assert not is_repo_only("crash")


def test_static_analysis_finds_defects_not_style(tmp_path):
    (tmp_path / "m.py").write_text("import os\ndef f(x, acc=[]):\n    acc.append(x)\n    return undefined_name + 1\n")
    found = {c.title.split(" in ")[0] for c in from_static_analysis(tmp_path)}
    assert any("mutable default" in t for t in found) and any("undefined name" in t for t in found)
    assert not any("imported but unused" in t for t in found)  # style/cleanup is not a defect


def test_rank_dedups_and_limits():
    a = Candidate("audit", "bug", "a", "", ["x.py"], 10, 0.9)
    b = Candidate("static", "bug", "b", "", ["x.py"], 11, 0.8)
    c = Candidate("static", "bug", "c", "", ["y.py"], 1, 0.7)
    assert [x.title for x in rank([c, b, a], 5)] == ["a", "c"]


def test_auto_mode_finds_fixes_and_opens_prs(tmp_path, monkeypatch):
    upstream, fork, gh_log = _fake_github(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    cfg.run.prepare_env = False
    cfg.run.sandbox = "none"  # the Docker path has its own test
    cfg.run.workspaces_dir = str(tmp_path / "ws")
    cfg.run.outputs_dir = str(tmp_path / "out")
    audit = {"findings": [{"file": "stats.py", "line": 2, "kind": "bug", "title": "mean divides by n-1",
                           "why": "mean() divides by len-1", "trigger": "mean([2, 4])", "expected": "3",
                           "actual": "6", "confidence": 0.9},
                          {"file": "stats.py", "line": 1, "kind": "bug", "title": "vague worry", "why": "?",
                           "trigger": "", "confidence": 0.9}]}  # no trigger: dropped
    provider = ScriptedProvider({"auditor": [audit], "coder": [
        [tc("edit_file", path="stats.py", old_str="(len(values) - 1)", new_str="len(values)"),
         tc("write_file", path="tests/test_stats.py", content="from stats import mean\n\ndef test_mean():\n    assert mean([2, 4]) == 3\n")],
        [tc("finish", summary="mean divided by n-1; now divides by n.")],
        [tc("edit_file", path="calc.py", old_str="def append_item(x, bucket=[]):\n",
            new_str="def append_item(x, bucket=None):\n    if bucket is None:\n        bucket = []\n"),
         tc("edit_file", path="tests/test_basic.py", old_str="    assert append_item(1) == [1]\n",
            new_str="    assert append_item(1) == [1]\n\ndef test_calls_do_not_share_state():\n    append_item(1)\n    assert append_item(2) == [2]\n")],
        [tc("finish", summary="The default list was shared between calls; use None.")],
    ]})
    out = auto_fix("https://github.com/acme/demo", cfg, provider, Console(quiet=True), explicit_pr=True)

    assert out["candidates"] == 2 and out["fixed"] == 2, json.dumps(out["results"], indent=1)
    assert out["prs"] == ["https://github.com/acme/demo/pull/1", "https://github.com/acme/demo/pull/2"]
    calls = gh_log.read_text()
    assert not any(line.startswith("pr merge") for line in calls.splitlines())
    create = [line for line in calls.splitlines() if line.startswith("pr create")]
    assert "--repo acme/demo --base main --head bot:lcc/auto-1-mean-divides-by-n-1" in create[0]
    assert calls.count("--draft") == 2  # maintainers see drafts first
    assert "## Verification" in create[0] or "Verification" in calls
    fork_branches = subprocess.run(["git", "branch", "--list"], cwd=fork, text=True, capture_output=True).stdout
    assert "lcc/auto-1-mean-divides-by-n-1" in fork_branches and "lcc/auto-2-" in fork_branches
    upstream_branches = subprocess.run(["git", "branch", "--list"], cwd=upstream, text=True, capture_output=True).stdout
    assert upstream_branches.split() == ["*", "main"]  # nothing pushed to a repo we may not write to
    first = next(b for b in fork_branches.split() if b.startswith("lcc/auto-1-"))
    diff = subprocess.run(["git", "show", "--stat", "--format=", first], cwd=fork, text=True, capture_output=True).stdout
    assert "stats.py" in diff and "calc.py" not in diff  # one fix per PR
    assert json.loads((tmp_path / "out" / f"auto-{Path(out['repo']).name}.json").read_text())["fixed"] == 2


def test_foreign_repo_needs_explicit_permission(tmp_path, monkeypatch):
    _upstream, fork, gh_log = _fake_github(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    cfg.run.prepare_env = False
    cfg.run.sandbox = "none"  # the Docker path has its own test
    cfg.run.workspaces_dir = str(tmp_path / "ws")
    cfg.run.outputs_dir = str(tmp_path / "out")
    cfg.auto.audit_calls = 0  # static analysis finds the mutable default
    provider = ScriptedProvider({"coder": [
        [tc("edit_file", path="calc.py", old_str="def append_item(x, bucket=[]):\n",
            new_str="def append_item(x, bucket=None):\n    if bucket is None:\n        bucket = []\n"),
         tc("edit_file", path="tests/test_basic.py", old_str="    assert append_item(1) == [1]\n",
            new_str="    assert append_item(1) == [1]\n\ndef test_no_shared_state():\n    append_item(1)\n    assert append_item(2) == [2]\n")],
        [tc("finish", summary="shared default list")]]})
    out = auto_fix("https://github.com/acme/demo", cfg, provider, Console(quiet=True))  # no PR=1, not a TTY
    assert out["fixed"] == 1 and out["prs"] == []
    assert "pr create" not in gh_log.read_text()
    assert subprocess.run(["git", "branch", "--list"], cwd=fork, text=True, capture_output=True).stdout == ""
