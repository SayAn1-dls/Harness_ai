from __future__ import annotations

import subprocess
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


def assert_permitted(action: GitHubPermission, granted: set[GitHubPermission] | None = None) -> None:
    allowed = granted or DEFAULT_PERMISSIONS
    if action not in allowed:
        raise PermissionError(f"GitHub action {action.value} is not granted")
    if action == GitHubPermission.MERGE_PR:
        raise PermissionError("merge is never granted to the default agent")


def create_pull_request(task: TaskState, title: str | None = None, body: str | None = None) -> str:
    assert_permitted(GitHubPermission.CREATE_PR)
    workspace = Path(task.workspace)
    title = title or f"{task.task_id}: {task.objective[:72]}"
    body = body or (
        f"Autonomous harness task `{task.task_id}`.\n\n"
        f"Objective: {task.objective}\n\n"
        f"Context snapshot: {task.context_snapshot}\n"
        f"Score: {task.global_score}\n\n"
        "Human approval is required before merge."
    )
    proc = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", body, "--head", task.branch],
        cwd=workspace,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "gh pr create failed")
    return proc.stdout.strip()
