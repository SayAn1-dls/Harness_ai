from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

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


# ------------------------------------------------------------------ who are we on GitHub?
API = "https://api.github.com"


def github_token() -> str:
    return (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()


def gh_login() -> str | None:
    """The account the GitHub CLI is signed in as, or None (not installed / not logged in)."""
    if not shutil.which("gh"):
        return None
    proc = subprocess.run(["gh", "api", "user", "--jq", ".login"], text=True, capture_output=True, timeout=60)
    login = proc.stdout.strip()
    return login if proc.returncode == 0 and login else None


def _api(method: str, path: str, token: str, payload: dict | None = None) -> tuple[int, Any]:
    """GitHub REST call with a token (used when the gh CLI is not available)."""
    r = httpx.request(method, f"{API}{path}", json=payload, timeout=30, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "lcc-harness"})
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text


def token_login(token: str) -> str | None:
    status, data = _api("GET", "/user", token)
    return data.get("login") if status == 200 and isinstance(data, dict) else None


def github_identity() -> tuple[str | None, str]:
    """(login, how): "gh" when the GitHub CLI is signed in, "token" for GH_TOKEN/GITHUB_TOKEN, else (None, "")."""
    login = gh_login()
    if login:
        return login, "gh"
    tok = github_token()
    if tok:
        login = token_login(tok)
        if login:
            return login, "token"
    return None, ""


def repo_access(repo: Path) -> tuple[str, bool, int]:
    """(owner/name, may the signed-in account push, how many open PRs that account already has there)."""
    slug = github_slug(repo)
    login, how = github_identity()
    if how == "token":
        tok = github_token()
        _, info = _api("GET", f"/repos/{slug}", tok)
        can_push = bool((info.get("permissions") or {}).get("push")) if isinstance(info, dict) else False
        _, prs = _api("GET", f"/repos/{slug}/pulls?state=open&per_page=100", tok)
        mine = sum(1 for p in prs if (p.get("user") or {}).get("login") == login) if isinstance(prs, list) else 0
        return slug, can_push, mine
    if not login:
        raise RuntimeError("no GitHub login: run `gh auth login`, or set GITHUB_TOKEN")
    can_push = _run(["gh", "api", f"repos/{slug}", "--jq", ".permissions.push"], repo, check=False).split()[:1] == ["true"]
    mine = _run(["gh", "pr", "list", "--repo", slug, "--author", "@me", "--state", "open", "--json", "number",
                 "--jq", "length"], repo, check=False)
    return slug, can_push, int(mine) if mine.isdigit() else 0


def _open_pr_with_token(repo: Path, branch: str, title: str, body: str, *, draft: bool, remote_branch: str,
                        base_branch: str | None) -> str:
    """Same as the gh path, through the REST API. The token reaches git through the environment (never argv)."""
    tok = github_token()
    login = token_login(tok)
    if not login:
        raise RuntimeError("GITHUB_TOKEN was rejected by GitHub")
    slug = github_slug(repo)
    status, info = _api("GET", f"/repos/{slug}", tok)
    if status != 200:
        raise RuntimeError(f"cannot read {slug} on GitHub ({status})")
    base = info.get("default_branch") or "main"
    if base_branch and base_branch != "HEAD" and _api("GET", f"/repos/{slug}/branches/{base_branch}", tok)[0] == 200:
        base = base_branch
    if (info.get("permissions") or {}).get("push"):
        owner, name = slug.split("/", 1)
    else:
        name = slug.split("/", 1)[1]
        _api("POST", f"/repos/{slug}/forks", tok, {})
        for _ in range(15):
            if _api("GET", f"/repos/{login}/{name}", tok)[0] == 200:
                break
            time.sleep(2)
        else:
            raise RuntimeError(f"could not create or find the fork {login}/{name}")
        owner = login
    basic = base64.b64encode(f"x-access-token:{tok}".encode()).decode()
    env = os.environ | {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}
    push = subprocess.run(["git", "-c", "credential.helper=", "push", "--force",
                           f"https://github.com/{owner}/{name}.git", f"{branch}:refs/heads/{remote_branch}"],
                          cwd=repo, text=True, capture_output=True, env=env, timeout=300)
    if push.returncode != 0:
        raise RuntimeError(f"git push failed: {push.stderr.strip()[-300:]}")
    head = remote_branch if owner == slug.split("/", 1)[0] else f"{owner}:{remote_branch}"
    _, open_prs = _api("GET", f"/repos/{slug}/pulls?state=open&head={owner}:{remote_branch}", tok)
    if isinstance(open_prs, list) and open_prs:
        return open_prs[0]["html_url"]
    status, pr = _api("POST", f"/repos/{slug}/pulls", tok,
                      {"title": title[:250], "head": head, "base": base, "body": body, "draft": draft})
    if status not in (200, 201):
        raise RuntimeError(f"GitHub refused the pull request ({status}): {str(pr)[:300]}")
    return pr["html_url"]


def open_pull_request(repo: Path, branch: str, title: str, body: str, *, draft: bool = False,
                      remote_branch: str | None = None, base_branch: str | None = None) -> str:
    """Push `branch` and open a PR against `base_branch` (when it exists on GitHub) or the default branch. Pushes to
    the repository itself when the signed-in account may, otherwise to a fork. Never merges. Returns the PR URL."""
    assert_permitted(GitHubPermission.CREATE_PR)
    remote_branch = remote_branch or branch
    if not gh_login():
        if github_token():
            return _open_pr_with_token(repo, branch, title, body, draft=draft, remote_branch=remote_branch,
                                       base_branch=base_branch)
        raise RuntimeError("no GitHub login: run `gh auth login` (or set GITHUB_TOKEN) so LCC can open the PR")
    slug = github_slug(repo)
    login = _run(["gh", "api", "user", "--jq", ".login"], repo)
    meta = _run(["gh", "api", f"repos/{slug}", "--jq", "[.permissions.push, .default_branch] | @tsv"], repo)
    can_push, _, base = meta.partition("\t")
    if base_branch and base_branch != "HEAD" and subprocess.run(
            ["gh", "api", f"repos/{slug}/branches/{base_branch}", "--silent"], cwd=repo, capture_output=True).returncode == 0:
        base = base_branch  # the branch the fix was built on, so the PR contains only the fix
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
          "push", "--force", remote, f"{branch}:refs/heads/{remote_branch}"], repo)  # lcc/* branches are ours
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
