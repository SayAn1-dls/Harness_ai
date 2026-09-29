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
  "api repos/acme/demo") printf '%s\tmain\n' "${GH_CAN_PUSH:-false}" ;;
  "api repos/acme/demo/branches/main") exit 0 ;;
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
    h = out["ai_honesty"]  # the model claimed 2 bugs: one had no trigger, the other was proven with a test
    assert (h["claimed"], h["dropped_no_trigger"], h["attempted"], h["proven"], h["rate"]) == (2, 1, 1, 1, 1.0)
    assert out["prs"] == ["https://github.com/acme/demo/pull/1", "https://github.com/acme/demo/pull/2"]
    calls = gh_log.read_text()
    assert not any(line.startswith("pr merge") for line in calls.splitlines())
    create = [line for line in calls.splitlines() if line.startswith("pr create")]
    assert "--repo acme/demo --base main --head bot:lcc/auto-1-mean-divides-by-n-1" in create[0]
    assert calls.count("--draft") == 2  # maintainers see drafts first
    assert calls.count("Verify it yourself") == 2 and "expected: FAIL (the bug is real)" in calls  # proof-carrying PRs
    assert "## Verification" in create[0] or "Verification" in calls
    fork_branches = subprocess.run(["git", "branch", "--list"], cwd=fork, text=True, capture_output=True).stdout
    assert "lcc/auto-1-mean-divides-by-n-1" in fork_branches and "lcc/auto-2-" in fork_branches
    upstream_branches = subprocess.run(["git", "branch", "--list"], cwd=upstream, text=True, capture_output=True).stdout
    assert upstream_branches.split() == ["*", "main"]  # nothing pushed to a repo we may not write to
    first = next(b for b in fork_branches.split() if b.startswith("lcc/auto-1-"))
    diff = subprocess.run(["git", "show", "--stat", "--format=", first], cwd=fork, text=True, capture_output=True).stdout
    assert "stats.py" in diff and "calc.py" not in diff  # one fix per PR
    assert json.loads((tmp_path / "out" / f"auto-{Path(out['repo']).name}.json").read_text())["fixed"] == 2
    report = (tmp_path / "out" / f"ISSUES-{Path(out['repo']).name}.md").read_text()
    assert report.count("fixed and proven") == 2 and "dropped: no way to trigger it" in report
    assert "The AI read **2 of 2** source files" in report and "pull/1" in report and "How often the AI was right" in report
    assert "Other issues found in this repository" in calls  # each PR lists what else was found


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


def test_make_run_fixes_an_issue_and_opens_a_pr_on_the_users_repo(tmp_path, monkeypatch):
    from lcc.session import fix_and_pr, parse_issue, resolve_repo

    upstream, _fork, gh_log = _fake_github(tmp_path, monkeypatch)
    monkeypatch.setenv("GH_CAN_PUSH", "true")  # the user's own repository
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    cfg.run.prepare_env = False
    cfg.run.sandbox = "none"
    cfg.run.workspaces_dir = str(tmp_path / "ws")
    cfg.run.outputs_dir = str(tmp_path / "out")
    issue = parse_issue("mean() returns the wrong value\n\nhttps://github.com/acme/demo/issues/7\n"
                        "Repository: https://github.com/acme/demo\n\nmean([2, 4]) returns 6, expected 3.")
    assert issue.number == 7 and issue.kind == "bug"
    repo = resolve_repo("https://github.com/acme/demo", tmp_path / "ws", Console(quiet=True))
    provider = ScriptedProvider({"coder": [
        [tc("edit_file", path="stats.py", old_str="(len(values) - 1)", new_str="len(values)"),
         tc("write_file", path="tests/test_stats.py", content="from stats import mean\n\ndef test_mean():\n    assert mean([2, 4]) == 3\n")],
        [tc("finish", summary="mean divided by n-1")]]})
    summary = fix_and_pr(issue, repo, cfg, provider, Console(quiet=True))
    assert summary["resolved"] and summary["pr"] == "https://github.com/acme/demo/pull/1", summary.get("pr_error")
    calls = gh_log.read_text()
    assert "--repo acme/demo --base main --head lcc/gh-7" in calls  # pushed to the user's repo, not a fork
    assert "Fixes #7" in calls and "--draft" in calls and "Verify it yourself" in calls
    branches = subprocess.run(["git", "branch", "--list"], cwd=upstream, text=True, capture_output=True).stdout
    assert "lcc/gh-7" in branches  # the fix branch is on GitHub, waiting for review
    assert not any(line.startswith("pr merge") for line in calls.splitlines())


def test_optimization_requests_are_recognised():
    from lcc.session import detect_kind

    assert detect_kind("Optimize parse_rows in loader.py", "") == "optimize"
    assert detect_kind("Report generation is too slow", "takes 40s on 10k rows") == "optimize"
    assert detect_kind("Speed up the search endpoint", "") == "optimize"
    assert detect_kind("Crash in the slow path", "") == "bug"  # a crash is a bug, even if it mentions slow
    assert detect_kind("mean() returns the wrong value", "") == "bug"


def test_github_login_is_asked_for_when_missing(monkeypatch):
    import lcc.github_pr as gp
    from lcc.session import ensure_github_login

    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(gp, "gh_login", lambda: None)
    monkeypatch.setattr(gp, "token_login", lambda tok: "alice" if tok == "good-token" else None)
    quiet = Console(quiet=True)
    assert ensure_github_login(quiet, interactive=False) is None  # never blocks a non-interactive run
    monkeypatch.setattr("builtins.input", lambda prompt="": "2")
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "good-token")
    assert ensure_github_login(quiet, interactive=True) == "alice"
    assert os.environ["GH_TOKEN"] == "good-token"  # this process only
    monkeypatch.delenv("GH_TOKEN")
    monkeypatch.setattr("builtins.input", lambda prompt="": "3")
    assert ensure_github_login(quiet, interactive=True) is None  # the user can skip PRs


def test_pr_with_only_a_token_no_gh_cli(tmp_path, monkeypatch):
    """A user with a GitHub token but no gh CLI: push to their fork through the REST API and open a draft PR."""
    import lcc.github_pr as gp
    from lcc.session import resolve_repo

    upstream, fork, _log = _fake_github(tmp_path, monkeypatch)
    monkeypatch.setattr(gp, "gh_login", lambda: None)
    monkeypatch.setenv("GH_TOKEN", "tok")
    calls = []

    def api(method, path, token, payload=None):
        calls.append((method, path, payload))
        if path == "/user":
            return 200, {"login": "bot"}
        if path == "/repos/acme/demo":
            return 200, {"default_branch": "main", "permissions": {"push": False}}
        if path == "/repos/bot/demo":
            return 200, {}
        if method == "POST" and path == "/repos/acme/demo/pulls":
            return 201, {"html_url": "https://github.com/acme/demo/pull/9"}
        return (202, {}) if method == "POST" else (200, [])

    monkeypatch.setattr(gp, "_api", api)
    repo = resolve_repo("https://github.com/acme/demo", tmp_path / "ws", Console(quiet=True))
    subprocess.run(["git", "checkout", "-q", "-b", "agent/T"], cwd=repo, check=True)
    (repo / "fix.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "fix"], cwd=repo, check=True)
    url = gp.open_pull_request(repo, "agent/T", "fix: thing", "body", draft=True, remote_branch="lcc/t")
    assert url == "https://github.com/acme/demo/pull/9"
    pr = next(p for m, path, p in calls if m == "POST" and path.endswith("/pulls"))
    assert pr["head"] == "bot:lcc/t" and pr["base"] == "main" and pr["draft"] is True
    assert "lcc/t" in subprocess.run(["git", "branch", "--list"], cwd=fork, text=True, capture_output=True).stdout
    assert subprocess.run(["git", "branch", "--list"], cwd=upstream, text=True, capture_output=True).stdout.split() == ["*", "main"]


def test_unverified_attempt_gets_one_draft_pr_marked_unverified(tmp_path, monkeypatch):
    _upstream, fork, gh_log = _fake_github(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    cfg = load_config()
    cfg.run.prepare_env = False
    cfg.run.sandbox = "none"
    cfg.run.max_iterations = 1
    cfg.run.workspaces_dir = str(tmp_path / "ws")
    cfg.run.outputs_dir = str(tmp_path / "out")
    cfg.auto.max_fixes = 1
    cfg.auto.pr_unverified = True
    audit = {"findings": [{"file": "stats.py", "line": 2, "kind": "bug", "title": "mean divides by n-1",
                           "why": "mean() divides by len-1", "trigger": "mean([2, 4])", "expected": "3",
                           "actual": "6", "confidence": 0.9}]}
    provider = ScriptedProvider({"auditor": [audit], "coder": [  # fixes the source but writes no test: not provable
        [tc("edit_file", path="stats.py", old_str="(len(values) - 1)", new_str="len(values)")],
        [tc("finish", summary="mean divided by n-1; now divides by n.")]]})
    out = auto_fix("https://github.com/acme/demo", cfg, provider, Console(quiet=True), explicit_pr=True)

    assert out["fixed"] == 0 and out["prs"] == ["https://github.com/acme/demo/pull/1"], json.dumps(out["results"], indent=1)
    assert out["results"][0]["unverified_pr"] is True
    create = [line for line in gh_log.read_text().splitlines() if line.startswith("pr create")]
    assert len(create) == 1 and "--title [unverified] fix: mean divides by n-1" in create[0]
    assert gh_log.read_text().count("--draft") == 1  # always a draft, whatever pr_draft says
    assert "**Unverified.**" in gh_log.read_text() and "Why it is not verified" in gh_log.read_text()
    report = (tmp_path / "out" / f"ISSUES-{Path(out['repo']).name}.md").read_text()
    assert "attempted, not proven" in report and "unverified draft PR" in report


def test_unverified_pr_is_off_by_default_and_needs_a_source_change(tmp_path):
    from lcc.config import AutoConfig
    from lcc.session import best_unverified

    assert AutoConfig().pr_unverified is False
    tests_only, source = tmp_path / "a.patch", tmp_path / "b.patch"
    tests_only.write_text("diff --git a/tests/test_x.py b/tests/test_x.py\n")
    source.write_text("diff --git a/src/x.ts b/src/x.ts\ndiff --git a/src/x.test.ts b/src/x.test.ts\n")
    a = {"resolved": False, "patch": str(tests_only), "verification": {"tests_run": 3, "tests_failed": 0}}
    b = {"resolved": False, "patch": str(source), "verification": {"tests_run": 0}}
    assert best_unverified([a]) is None  # a PR of tests alone fixes nothing
    assert best_unverified([a, b]) is b
    assert best_unverified([dict(b, fatal=True)]) is None


def test_noisy_static_rules_rank_below_a_triggered_model_finding(tmp_path):
    (tmp_path / "m.py").write_text("def q(t):\n    return f\"SELECT * FROM {t}\"\n")
    static = [c for c in from_static_analysis(tmp_path) if "SQL" in c.title]
    audit = Candidate("audit", "bug", "real bug", "", ["n.py"], 3, 0.6)
    assert static and [c.title for c in rank(static + [audit], 1)] == ["real bug"]
