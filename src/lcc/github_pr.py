from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

from lcc.schemas import GitHubPermission, TaskState

DEFAULT_PERMISSIONS = {
    GitHubPermission.READ_REPO,
    GitHubPermission.READ_ISSUES,
    GitHubPermission.READ_PRS,
    GitHubPermission.CREATE_BRANCH,
    GitHubPermission.WRITE_BRANCH,
    GitHubPermission.CREATE_PR,
    GitHubPermission.COMMENT_PR,
}
GITHUB_URL = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
FORK_REMOTE = "lcc-fork"


def assert_permitted(action: GitHubPermission, granted: set[GitHubPermission] | None = None) -> None:
    allowed = granted or DEFAULT_PERMISSIONS
    if action not in allowed:
        raise PermissionError(f"GitHub action {action.value} is not granted")
    if action == GitHubPermission.MERGE_PR:
        raise PermissionError("merge is never granted to the default agent")


def _run(cmd: list[str], cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=300)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {(proc.stderr or proc.stdout).strip()[-500:]}")
    return proc.stdout.strip()


def github_slug(repo: Path) -> str:
    url = _run(["git", "config", "--get", "remote.origin.url"], repo, check=False)  # raw, before insteadOf
    m = GITHUB_URL.search(url)
    if not m:
        raise RuntimeError(f"origin is not a GitHub repository ({url or 'no origin remote'}); cannot open a PR")
    return f"{m.group(1)}/{m.group(2)}"


def open_pull_request(repo: Path, branch: str, title: str, body: str, *, draft: bool = False,
                      remote_branch: str | None = None) -> str:
    """Push `branch` and open a PR against the repository's default branch. Pushes to the repository itself
    when the signed-in account may, otherwise to a fork. Never merges. Returns the PR URL."""
    assert_permitted(GitHubPermission.CREATE_PR)
    if not shutil.which("gh"):
        raise RuntimeError("the GitHub CLI `gh` is required to open pull requests (install it and run `gh auth login`)")
    slug = github_slug(repo)
    remote_branch = remote_branch or branch
    login = _run(["gh", "api", "user", "--jq", ".login"], repo)
    meta = _run(["gh", "api", f"repos/{slug}", "--jq", "[.permissions.push, .default_branch] | @tsv"], repo)
    can_push, _, base = meta.partition("\t")
    if can_push.strip() == "true":
        remote, head = "origin", remote_branch
    else:
        name = slug.split("/", 1)[1]
        fork = f"{login}/{name}"
        _run(["gh", "repo", "fork", slug, "--clone=false", "--remote=false"], repo, check=False)
        for _ in range(15):  # forks are created asynchronously
            if subprocess.run(["gh", "api", f"repos/{fork}", "--silent"], cwd=repo, capture_output=True).returncode == 0:
                break
            time.sleep(2)
        else:
            raise RuntimeError(f"could not create or find the fork {fork}")
        url = f"https://github.com/{fork}.git"
        if _run(["git", "remote", "get-url", FORK_REMOTE], repo, check=False):
            _run(["git", "remote", "set-url", FORK_REMOTE, url], repo)
        else:
            _run(["git", "remote", "add", FORK_REMOTE, url], repo)
        remote, head = FORK_REMOTE, f"{login}:{remote_branch}"
    # gh supplies the credentials, whatever helper git is configured with.
    _run(["git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
          "push", "--force-with-lease", remote, f"{branch}:refs/heads/{remote_branch}"], repo)
    existing = _run(["gh", "pr", "list", "--repo", slug, "--head", remote_branch, "--state", "open",
                     "--json", "url", "--jq", ".[0].url"], repo, check=False)
    if existing:
        return existing
    cmd = ["gh", "pr", "create", "--repo", slug, "--base", base.strip() or "main", "--head", head,
           "--title", title[:250], "--body", body]
    if draft:
        cmd.append("--draft")
    out = _run(cmd, repo)
    return out.splitlines()[-1] if out else ""


def create_pull_request(task: TaskState, title: str | None = None, body: str | None = None) -> str:
    """`lcc pr` after `lcc approve`: open a PR for a verified task branch."""
    title = title or f"{task.task_id}: {task.objective[:72]}"
    body = body or (
        f"Autonomous harness task `{task.task_id}`.\n\n"
        f"Objective: {task.objective}\n\n"
        f"Context snapshot: {task.context_snapshot}\n"
        f"Score: {task.global_score}\n\n"
        "Human approval is required before merge."
    )
    return open_pull_request(Path(task.workspace), task.branch, title, body)
