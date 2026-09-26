import shutil
import subprocess
from pathlib import Path

from rich.console import Console

from lcc.config import load_config
from lcc.model import MockProvider
from lcc.session import parse_issue, solve


def test_parse_issue_text_and_file(tmp_path):
    issue = parse_issue("# Fix add\nRepository: https://github.com/o/r\n\nadd(2,3) should be 5\nSee https://github.com/o/r/issues/12")
    assert issue.title == "Fix add"
    assert issue.repo_hint == "https://github.com/o/r"
    assert issue.task_id == "GH-12"
    f = tmp_path / "issue.md"
    f.write_text("Crash on empty input\n\nbody")
    assert parse_issue(str(f)).title == "Crash on empty input"


def test_solve_plain_directory_restores_branch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    shutil.copytree(Path(__file__).parent / "fixtures" / "mini_repo", repo)
    cfg = load_config()
    cfg.run.prepare_env = False
    cfg.run.outputs_dir = str(tmp_path / "out")
    issue = parse_issue("Fix add so it returns the sum\n\nadd(2, 3) should be 5")
    summary = solve(issue, repo, cfg, MockProvider(), Console(quiet=True))
    assert summary["resolved"] is True
    assert "return a + b" in Path(summary["patch"]).read_text()
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=repo, text=True, capture_output=True).stdout.strip()
    assert branch == "main"
    assert (repo / "app.py").read_text().count("a - b") == 1  # original branch untouched; fix lives on agent/*


def _git_repo(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(Path(__file__).parent / "fixtures" / "mini_repo", repo)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=u", "-c", "user.email=u@x", "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def _cfg(tmp_path):
    cfg = load_config()
    cfg.run.prepare_env = False
    cfg.run.outputs_dir = str(tmp_path / "out")
    return cfg


def test_solve_keeps_uncommitted_user_changes_out_of_the_fix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = _git_repo(tmp_path)
    (repo / "NOTES.txt").write_text("user wip\n")
    (repo / "README.md").write_text((repo / "README.md").read_text() + "\nuser edit\n")
    issue = parse_issue("Fix add so it returns the sum\n\nadd(2, 3) should be 5")
    summary = solve(issue, repo, _cfg(tmp_path), MockProvider(), Console(quiet=True))
    assert summary["resolved"] is True
    assert (repo / "NOTES.txt").read_text() == "user wip\n"
    assert "user edit" in (repo / "README.md").read_text()
    changed = subprocess.run(["git", "diff", "--name-only", "main", summary["branch"]], cwd=repo, text=True, capture_output=True).stdout.split()
    assert changed == ["app.py"]
    assert "NOTES" not in Path(summary["patch"]).read_text()


def test_rerun_of_same_issue_gets_a_fresh_branch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = _git_repo(tmp_path)
    text = "Fix add so it returns the sum\n\nadd(2, 3) should be 5\nhttps://github.com/o/r/issues/7"
    first = solve(parse_issue(text), repo, _cfg(tmp_path), MockProvider(), Console(quiet=True))
    second = solve(parse_issue(text), repo, _cfg(tmp_path), MockProvider(), Console(quiet=True))
    assert (first["task_id"], first["resolved"]) == ("GH-7", True)
    assert (second["task_id"], second["resolved"]) == ("GH-7-2", True)
    assert "return a + b" in Path(first["patch"]).read_text()


def test_solve_repo_without_commits(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    repo = tmp_path / "repo"
    shutil.copytree(Path(__file__).parent / "fixtures" / "mini_repo", repo)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    issue = parse_issue("Fix add so it returns the sum\n\nadd(2, 3) should be 5")
    summary = solve(issue, repo, _cfg(tmp_path), MockProvider(), Console(quiet=True))
    assert summary["resolved"] is True
    assert subprocess.run(["git", "branch", "--show-current"], cwd=repo, text=True, capture_output=True).stdout.strip() == "main"


def test_start_with_empty_piped_issue_fails(monkeypatch):
    import io

    from lcc.session import start

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert start(None, None, "mock", once=False) == 2
