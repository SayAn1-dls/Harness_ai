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
