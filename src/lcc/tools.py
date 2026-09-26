from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from lcc.schemas import Budget, TaskState

ToolFn = Callable[..., dict[str, Any]]


class ToolError(RuntimeError):
    pass


class ToolBudgetExceeded(RuntimeError):
    pass


@dataclass
class ToolSpec:
    name: str
    description: str
    fn: ToolFn
    risk: str = "low"
    cost: int = 1


class ToolPolicy:
    def __init__(self, permissions: dict[str, bool] | None = None) -> None:
        self.permissions = {
            "read_repository": True,
            "write_repository": False,
            "shell": False,
            "network": False,
            "git_write": False,
            **(permissions or {}),
        }

    def require(self, key: str) -> None:
        if not self.permissions.get(key, False):
            raise ToolError(f"permission denied: {key}")


class ToolRegistry:
    def __init__(self, workspace: Path, policy: ToolPolicy, budget: Budget) -> None:
        self.workspace = Path(workspace).resolve()
        self.policy = policy
        self.budget = budget
        self.specs: dict[str, ToolSpec] = {}
        self._register_defaults()

    def _register(self, spec: ToolSpec) -> None:
        self.specs[spec.name] = spec

    def names(self) -> list[str]:
        return sorted(self.specs)

    def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        if name not in self.specs:
            raise ToolError(f"unknown tool: {name}")
        return self.specs[name].fn(**kwargs)

    def _charge(self, name: str) -> None:
        if name in {"search_code", "find_symbol", "find_references"}:
            self.budget.search_calls += 1
            if self.budget.search_calls > self.budget.max_search_calls:
                raise ToolBudgetExceeded("search budget exhausted")
        if name in {"read_file"}:
            self.budget.file_reads += 1
            if self.budget.file_reads > self.budget.max_file_reads:
                raise ToolBudgetExceeded("file-read budget exhausted")
        if name in {"shell", "run_test", "run_lint", "run_typecheck"}:
            self.budget.shell_calls += 1
            if self.budget.shell_calls > self.budget.max_shell_calls:
                raise ToolBudgetExceeded("shell budget exhausted")

    def _safe_path(self, rel: str) -> Path:
        path = (self.workspace / rel).resolve()
        if not str(path).startswith(str(self.workspace)):
            raise ToolError("path escapes workspace")
        return path

    def _register_defaults(self) -> None:
        self._register(ToolSpec("repo_tree", "List repository files", self.repo_tree))
        self._register(ToolSpec("search_code", "Search code for a query", self.search_code, cost=2))
        self._register(ToolSpec("find_symbol", "Find a symbol by name", self.find_symbol))
        self._register(ToolSpec("find_references", "Find files mentioning a symbol", self.find_references))
        self._register(ToolSpec("read_file", "Read a file slice", self.read_file, cost=2))
        self._register(ToolSpec("apply_patch", "Apply a unified diff", self.apply_patch, risk="high", cost=4))
        self._register(ToolSpec("git_diff", "Show git diff", self.git_diff))
        self._register(ToolSpec("run_test", "Run tests", self.run_test, risk="medium", cost=8))
        self._register(ToolSpec("run_lint", "Run linters", self.run_lint, risk="low", cost=4))
        self._register(ToolSpec("run_typecheck", "Run typecheck", self.run_typecheck, cost=4))
        self._register(ToolSpec("shell", "Restricted shell command", self.shell, risk="high", cost=6))
        self._register(ToolSpec("write_file", "Write a whole file", self.write_file, risk="high", cost=3))

    def repo_tree(self, max_entries: int = 400) -> dict[str, Any]:
        self.policy.require("read_repository")
        files: list[str] = []
        for p in self.workspace.rglob("*"):
            if p.is_file() and ".git" not in p.parts and "harness/cache" not in p.as_posix():
                files.append(p.relative_to(self.workspace).as_posix())
                if len(files) >= max_entries:
                    break
        return {"files": files}

    def search_code(self, query: str, max_hits: int = 20) -> dict[str, Any]:
        self._charge("search_code")
        self.policy.require("read_repository")
        hits: list[dict[str, Any]] = []
        for p in self.workspace.rglob("*"):
            if not p.is_file() or ".git" in p.parts:
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if query.lower() in text.lower():
                line_no = next(
                    (i + 1 for i, line in enumerate(text.splitlines()) if query.lower() in line.lower()),
                    1,
                )
                hits.append({"path": p.relative_to(self.workspace).as_posix(), "line": line_no})
                if len(hits) >= max_hits:
                    break
        return {"hits": hits}

    def find_symbol(self, name: str) -> dict[str, Any]:
        return self.search_code(name)

    def find_references(self, symbol: str) -> dict[str, Any]:
        return self.search_code(symbol)

    def read_file(self, path: str, start: int = 1, end: int = 200) -> dict[str, Any]:
        self._charge("read_file")
        self.policy.require("read_repository")
        file = self._safe_path(path)
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        sl = max(1, start)
        el = min(len(lines), end)
        numbered = "\n".join(f"{i}|{lines[i-1]}" for i in range(sl, el + 1))
        return {"path": path, "start": sl, "end": el, "content": numbered}

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        self.policy.require("write_repository")
        file = self._safe_path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
        return {"path": path, "bytes": len(content.encode())}

    def apply_patch(self, diff: str) -> dict[str, Any]:
        self.policy.require("write_repository")
        proc = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            cwd=self.workspace,
            input=diff,
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            # fallback: simple whole-file writes encoded in a custom patch format
            applied = _apply_simple_patch(self.workspace, diff)
            if not applied:
                return {"ok": False, "stderr": proc.stderr, "stdout": proc.stdout}
            return {"ok": True, "method": "simple", "files": applied}
        return {"ok": True, "method": "git_apply"}

    def git_diff(self) -> dict[str, Any]:
        self.policy.require("read_repository")
        proc = subprocess.run(["git", "diff"], cwd=self.workspace, text=True, capture_output=True)
        return {"diff": proc.stdout, "stderr": proc.stderr}

    def run_test(self, target: str = "") -> dict[str, Any]:
        self._charge("run_test")
        self.policy.require("shell")
        self.budget.full_test_runs += 1
        if self.budget.full_test_runs > self.budget.max_full_test_runs:
            raise ToolBudgetExceeded("test-run budget exhausted")
        cmd = _detect_test_cmd(self.workspace, target)
        return self._run(cmd)

    def run_lint(self) -> dict[str, Any]:
        self._charge("run_lint")
        self.policy.require("shell")
        if (self.workspace / "pyproject.toml").exists():
            return self._run([sys.executable, "-m", "ruff", "check", "."])
        return {"ok": True, "skipped": True, "reason": "no linter configured"}

    def run_typecheck(self) -> dict[str, Any]:
        self.policy.require("shell")
        return {"ok": True, "skipped": True, "reason": "typecheck optional"}

    def shell(self, command: str) -> dict[str, Any]:
        self._charge("shell")
        self.policy.require("shell")
        if _dangerous(command):
            raise ToolError("command blocked by policy")
        return self._run(["/bin/sh", "-c", command])

    def _run(self, cmd: list[str]) -> dict[str, Any]:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(self.workspace) + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(cmd, cwd=self.workspace, text=True, capture_output=True, env=env, timeout=120)
        return {
            "ok": proc.returncode == 0,
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout": proc.stdout[-8000:],
            "stderr": proc.stderr[-8000:],
        }


def _dangerous(command: str) -> bool:
    banned = ("rm -rf /", "sudo ", "mkfs", "dd if=", "shutdown", "reboot", "curl | sh", "wget | sh")
    c = command.lower()
    return any(b in c for b in banned)


def _detect_test_cmd(workspace: Path, target: str) -> list[str]:
    py = sys.executable
    if target:
        if target.endswith(".py"):
            return [py, "-m", "unittest", target.replace("/", ".").removesuffix(".py")]
        return [py, "-m", "unittest", target]
    tests = list(workspace.glob("tests/test_*.py")) + list(workspace.glob("test_*.py"))
    if tests:
        if (workspace / "tests").is_dir():
            return [py, "-m", "unittest", "discover", "-s", "tests", "-q"]
        return [py, "-m", "unittest", "discover", "-q"]
    if (workspace / "package.json").exists():
        return ["npm", "test", "--silent"]
    return [py, "-m", "unittest", "discover", "-q"]


def _apply_simple_patch(root: Path, diff: str) -> list[str]:
    """Support *** ADD path / *** BODY blocks for mock models."""
    files: list[str] = []
    blocks = diff.split("*** ADD ")
    for block in blocks[1:]:
        header, _, body = block.partition("\n")
        path = header.strip()
        dest = (root / path).resolve()
        if not str(dest).startswith(str(root.resolve())):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        files.append(path)
    blocks = diff.split("*** REPLACE ")
    for block in blocks[1:]:
        header, _, rest = block.partition("\n")
        path = header.strip()
        if "\n*** WITH\n" in rest:
            old, _, new = rest.partition("\n*** WITH\n")
            dest = (root / path).resolve()
            if dest.exists():
                text = dest.read_text(encoding="utf-8")
                dest.write_text(text.replace(old, new, 1), encoding="utf-8")
                files.append(path)
        else:
            dest = (root / path).resolve()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(rest, encoding="utf-8")
            files.append(path)
    return files


def charge_full_test(task: TaskState) -> None:
    task.budget.full_test_runs += 1
