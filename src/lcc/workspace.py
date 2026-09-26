from __future__ import annotations

import os
import subprocess
from pathlib import Path

from lcc.schemas import TaskState
from lcc.tools import ToolError


class Workspace:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()

    def ensure_git(self) -> str:
        git = self.path / ".git"
        if not git.exists():
            proc = subprocess.run(["git", "init", "-b", "main"], cwd=self.path, capture_output=True)
            if proc.returncode != 0:
                subprocess.run(["git", "init"], cwd=self.path, check=True, capture_output=True)
            subprocess.run(["git", "add", "-A", "--", ".", ":!harness"], cwd=self.path, capture_output=True)
            self._commit("lcc: workspace snapshot")
        return self.head()

    def head(self) -> str:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.path, text=True, capture_output=True)
        return (proc.stdout or "").strip() or "unknown"

    def create_task_branch(self, task: TaskState) -> str:
        if not task.branch:
            safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in task.task_id)
            task.branch = f"agent/{safe}"
        existing = subprocess.run(
            ["git", "rev-parse", "--verify", task.branch],
            cwd=self.path,
            capture_output=True,
            text=True,
        )
        if existing.returncode != 0:
            subprocess.run(["git", "checkout", "-b", task.branch], cwd=self.path, check=True, capture_output=True)
        else:
            subprocess.run(["git", "checkout", task.branch], cwd=self.path, capture_output=True)
        return task.branch

    def _git_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "lcc",
                "GIT_AUTHOR_EMAIL": "lcc@local",
                "GIT_COMMITTER_NAME": "lcc",
                "GIT_COMMITTER_EMAIL": "lcc@local",
            }
        )
        return env

    def _commit(self, message: str) -> None:
        proc = subprocess.run(
            ["git", "commit", "-m", message, "--allow-empty"],
            cwd=self.path,
            capture_output=True,
            text=True,
            env=self._git_env(),
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git commit failed: {proc.stderr or proc.stdout}")

    def commit(self, message: str) -> str:
        """Commit task changes on the task branch. Harness state is never committed."""
        if self.current_branch() in {"main", "master", "trunk"}:
            raise ToolError("refusing to commit on a protected branch")
        subprocess.run(["git", "add", "-A", "--", ".", ":!harness"], cwd=self.path, capture_output=True)
        self._commit(message)
        return self.head()

    def current_branch(self) -> str:
        proc = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.path, text=True, capture_output=True)
        return proc.stdout.strip()

    def reset_hard(self, rev: str) -> None:
        """Discard the attempt: tracked changes and files the agent created (harness state is kept)."""
        subprocess.run(["git", "reset", "--hard", rev], cwd=self.path, check=True, capture_output=True)
        subprocess.run(["git", "clean", "-fd", "-e", "harness"], cwd=self.path, capture_output=True)


def assert_not_main(branch: str) -> None:
    if branch in {"main", "master", "trunk"}:
        raise ToolError("coding agent must not use main/master/trunk as the task branch")
