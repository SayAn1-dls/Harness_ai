from __future__ import annotations

import ast
import fnmatch
import importlib.util
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from lcc.schemas import Budget, TaskState

if TYPE_CHECKING:
    from lcc.context_engine import RepoIndex

ToolFn = Callable[..., dict[str, Any]]

SKIP_DIRS = {".git", "harness", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", "dist", "build"}
READ_WINDOW = 200
WRITE_TOOLS = {"write_file", "edit_file", "apply_patch"}
DEFAULT_FORBIDDEN = ["harness/**", ".git/**"]


class ToolError(RuntimeError):
    pass


class ToolBudgetExceeded(RuntimeError):
    pass


def _p(type_: str, desc: str, **extra: Any) -> dict[str, Any]:
    return {"type": type_, "description": desc, **extra}


def _schema(required: list[str], **props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


@dataclass
class ToolSpec:
    name: str
    description: str
    fn: ToolFn
    params: dict[str, Any] = field(default_factory=lambda: _schema([]))
    risk: str = "low"
    cost: int = 1

    def openai_schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.params}}


class ToolPolicy:
    """Permissions plus path scope. Forbidden paths are hard; out-of-plan writes are recorded (scope_mode=record)
    or refused (scope_mode=strict)."""

    def __init__(
        self,
        permissions: dict[str, bool] | None = None,
        *,
        allowed_paths: list[str] | None = None,
        forbidden_paths: list[str] | None = None,
        scope_mode: str = "record",
    ) -> None:
        self.permissions = {
            "read_repository": True,
            "write_repository": False,
            "run_tests": False,
            "shell": False,
            "network": False,
            "git_write": False,
            **(permissions or {}),
        }
        self.allowed_paths = list(allowed_paths or [])
        self.forbidden_paths = DEFAULT_FORBIDDEN + list(forbidden_paths or [])
        self.scope_mode = scope_mode

    def require(self, key: str) -> None:
        if not self.permissions.get(key, False):
            raise ToolError(f"permission denied: {key}")

    def check_write(self, rel: str) -> bool:
        """Raise if forbidden. Return True when the path is outside the plan's allowed files."""
        if any(_match(rel, pat) for pat in self.forbidden_paths):
            raise ToolError(f"path is forbidden by policy: {rel}")
        if not self.allowed_paths or any(_match(rel, pat) for pat in self.allowed_paths):
            return False
        if self.scope_mode == "strict":
            raise ToolError(f"{rel} is outside the plan's allowed files {self.allowed_paths}")
        return True


def _match(rel: str, pattern: str) -> bool:
    return rel == pattern or fnmatch.fnmatch(rel, pattern) or (pattern.endswith("/**") and rel.startswith(pattern[:-2]))


class ToolRegistry:
    def __init__(self, workspace: Path, policy: ToolPolicy, budget: Budget, index: RepoIndex | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.policy = policy
        self.budget = budget
        self.index = index
        self.specs: dict[str, ToolSpec] = {}
        self.changed: list[str] = []
        self.scope_expansions: list[str] = []
        self._register_defaults()

    def _register(self, spec: ToolSpec) -> None:
        self.specs[spec.name] = spec

    def names(self) -> list[str]:
        return sorted(self.specs)

    def schemas(self, names: list[str]) -> list[dict[str, Any]]:
        return [self.specs[n].openai_schema() for n in names if n in self.specs]

    def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        if name not in self.specs:
            raise ToolError(f"unknown tool: {name}")
        self.budget.tool_calls += 1
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
        if not path.is_relative_to(self.workspace):
            raise ToolError("path escapes workspace")
        return path

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.workspace).as_posix()

    def _iter_files(self):
        for p in sorted(self.workspace.rglob("*")):
            if not p.is_file():
                continue
            parts = p.relative_to(self.workspace).parts
            if any(part in SKIP_DIRS for part in parts[:-1]) or p.suffix in {".pyc"}:
                continue
            yield p

    def _register_defaults(self) -> None:
        path = _p("string", "Repository-relative file path")
        R = self._register
        R(ToolSpec("repo_tree", "List repository files (skips vendored/harness dirs).", self.repo_tree,
                   _schema([], max_entries=_p("integer", "Max entries (default 300)"))))
        R(ToolSpec("get_repo_map", "Ranked map of important files and their symbols. Cheap orientation.", self.get_repo_map))
        R(ToolSpec("search_code", "Case-insensitive literal search. Returns path:line: text hits.", self.search_code,
                   _schema(["query"], query=_p("string", "Text to search for"), max_hits=_p("integer", "Default 30")), cost=2))
        R(ToolSpec("find_symbol", "Find where a function/class is defined.", self.find_symbol,
                   _schema(["name"], name=_p("string", "Symbol name"))))
        R(ToolSpec("find_references", "Find usages of an identifier (whole-word).", self.find_references,
                   _schema(["symbol"], symbol=_p("string", "Identifier"))))
        R(ToolSpec("read_file", f"Read numbered lines of a file (max {READ_WINDOW} lines per call).", self.read_file,
                   _schema(["path"], path=path, start=_p("integer", "First line, 1-based"), end=_p("integer", "Last line")), cost=2))
        R(ToolSpec("edit_file", "Replace one exact, unique occurrence of old_str with new_str. Include enough surrounding "
                   "lines to make old_str unique. Python edits are syntax-checked.", self.edit_file,
                   _schema(["path", "old_str", "new_str"], path=path, old_str=_p("string", "Exact existing text"),
                           new_str=_p("string", "Replacement text")), risk="high", cost=3))
        R(ToolSpec("write_file", "Create a new file or fully overwrite a small file.", self.write_file,
                   _schema(["path", "content"], path=path, content=_p("string", "Full file content")), risk="high", cost=3))
        R(ToolSpec("apply_patch", "Apply a unified diff (git apply).", self.apply_patch,
                   _schema(["diff"], diff=_p("string", "Unified diff")), risk="high", cost=4))
        R(ToolSpec("git_diff", "Show the current uncommitted diff of the task branch.", self.git_diff))
        R(ToolSpec("run_test", "Run tests. Pass a test file path (e.g. tests/test_x.py) for a focused run.", self.run_test,
                   _schema([], target=_p("string", "Optional test file or node id")), risk="medium", cost=8))
        R(ToolSpec("run_lint", "Run the configured linter.", self.run_lint, cost=4))
        R(ToolSpec("run_typecheck", "Run the configured typechecker.", self.run_typecheck, cost=4))
        R(ToolSpec("shell", "Restricted shell command.", self.shell,
                   _schema(["command"], command=_p("string", "Command")), risk="high", cost=6))
        R(ToolSpec("finish", "Call when done. Summarize what you did and why.", lambda **kw: {"done": True, **kw},
                   _schema(["summary"], summary=_p("string", "What changed and why"),
                           files=_p("array", "Optional list of relevant file paths", items={"type": "string"}))))

    # ---- read tools -------------------------------------------------
    def repo_tree(self, max_entries: int = 300) -> dict[str, Any]:
        self.policy.require("read_repository")
        files = [self._rel(p) for p in self._iter_files()]
        return {"files": files[:max_entries], "total": len(files)}

    def get_repo_map(self) -> dict[str, Any]:
        self.policy.require("read_repository")
        from lcc.context_engine import repo_map, scan_repo

        self.index = self.index or scan_repo(self.workspace)
        return {"map": repo_map(self.index, token_budget=1500)}

    def search_code(self, query: str, max_hits: int = 30) -> dict[str, Any]:
        self._charge("search_code")
        self.policy.require("read_repository")
        return {"hits": self._grep(lambda line: query.lower() in line.lower(), max_hits)}

    def _grep(self, pred: Callable[[str], bool], max_hits: int) -> list[str]:
        hits: list[str] = []
        for p in self._iter_files():
            try:
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if pred(line):
                    hits.append(f"{self._rel(p)}:{i}: {line.strip()[:160]}")
                    if len(hits) >= max_hits:
                        return hits
        return hits

    def find_symbol(self, name: str) -> dict[str, Any]:
        self._charge("find_symbol")
        self.policy.require("read_repository")
        if self.index is not None:
            defs = [f"{s.path}:{s.line}: {s.kind} {s.name}" for s in self.index.symbols if s.name == name]
            if defs:
                return {"definitions": defs}
        pat = re.compile(rf"^\s*(?:export\s+)?(?:async\s+)?(?:def|class|function)\s+{re.escape(name)}\b")
        return {"definitions": self._grep(lambda line: bool(pat.search(line)), 20)}

    def find_references(self, symbol: str) -> dict[str, Any]:
        self._charge("find_references")
        self.policy.require("read_repository")
        pat = re.compile(rf"\b{re.escape(symbol)}\b")
        return {"references": self._grep(lambda line: bool(pat.search(line)), 40)}

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> dict[str, Any]:
        self._charge("read_file")
        self.policy.require("read_repository")
        file = self._safe_path(path)
        if not file.is_file():
            raise ToolError(f"no such file: {path}")
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        sl = max(1, int(start or 1))
        el = min(len(lines), int(end or sl + READ_WINDOW - 1), sl + READ_WINDOW - 1)
        numbered = "\n".join(f"{i:>4}| {lines[i - 1]}" for i in range(sl, el + 1))
        more = f"\n[... {len(lines) - el} more lines; call read_file with start={el + 1}]" if el < len(lines) else ""
        return {"path": path, "total_lines": len(lines), "content": numbered + more}

    # ---- write tools ------------------------------------------------
    def _before_write(self, rel: str) -> Path:
        self.policy.require("write_repository")
        file = self._safe_path(rel)
        rel = self._rel(file)
        if self.policy.check_write(rel) and rel not in self.scope_expansions:
            self.scope_expansions.append(rel)
        return file

    def _after_write(self, rel: str) -> None:
        if rel not in self.changed:
            self.changed.append(rel)

    def edit_file(self, path: str, old_str: str, new_str: str) -> dict[str, Any]:
        file = self._before_write(path)
        if not file.is_file():
            raise ToolError(f"no such file: {path}; use write_file to create it")
        text = file.read_text(encoding="utf-8")
        count = text.count(old_str) if old_str else 0
        if count == 0:
            raise ToolError("old_str not found. Re-read the file and copy the exact text (whitespace matters).")
        if count > 1:
            raise ToolError(f"old_str occurs {count} times; include more surrounding lines to make it unique.")
        new_text = text.replace(old_str, new_str, 1)
        _check_syntax(path, new_text)
        file.write_text(new_text, encoding="utf-8")
        self._after_write(path)
        line = text[: text.index(old_str)].count("\n") + 1
        lines = new_text.splitlines()
        lo, hi = max(1, line - 3), min(len(lines), line + new_str.count("\n") + 3)
        return {"ok": True, "path": path, "snippet": "\n".join(f"{i:>4}| {lines[i - 1]}" for i in range(lo, hi + 1))}

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        file = self._before_write(path)
        _check_syntax(path, content)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content, encoding="utf-8")
        self._after_write(path)
        return {"ok": True, "path": path, "bytes": len(content.encode())}

    def apply_patch(self, diff: str) -> dict[str, Any]:
        self.policy.require("write_repository")
        targets = re.findall(r"^\+\+\+ (?:b/)?(\S+)", diff, re.M)
        for rel in targets:
            if rel != "/dev/null":
                self._before_write(rel)
        proc = subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], cwd=self.workspace, input=diff, text=True, capture_output=True)
        if proc.returncode != 0:
            applied = _apply_simple_patch(self.workspace, diff)
            if not applied:
                return {"ok": False, "stderr": proc.stderr[-2000:]}
            targets = applied
        for rel in targets:
            if rel != "/dev/null":
                self._after_write(rel)
        return {"ok": True, "files": targets}

    def git_diff(self) -> dict[str, Any]:
        self.policy.require("read_repository")
        return {"diff": workspace_diff(self.workspace)}

    # ---- execution tools --------------------------------------------
    def run_test(self, target: str = "") -> dict[str, Any]:
        self._charge("run_test")
        self.policy.require("run_tests")
        if not target:
            self.budget.full_test_runs += 1
            if self.budget.full_test_runs > self.budget.max_full_test_runs:
                raise ToolBudgetExceeded("test-run budget exhausted")
        res = self._run(detect_test_cmd(self.workspace, target))
        run, failed = parse_test_counts(res["stdout"] + "\n" + res["stderr"])
        res["tests_run"] = run
        res["tests_failed"] = failed
        res["ok"] = res["returncode"] == 0 and run > 0
        return res

    def run_lint(self) -> dict[str, Any]:
        self._charge("run_lint")
        self.policy.require("run_tests")
        if importlib.util.find_spec("ruff") is None:
            return {"ok": True, "skipped": True, "reason": "ruff not installed"}
        return self._run([sys.executable, "-m", "ruff", "check", "--select", "E9,F63,F7,F82", "."])

    def run_typecheck(self) -> dict[str, Any]:
        self.policy.require("run_tests")
        return {"ok": True, "skipped": True, "reason": "typecheck not configured"}

    def shell(self, command: str) -> dict[str, Any]:
        self._charge("shell")
        self.policy.require("shell")
        if _dangerous(command):
            raise ToolError("command blocked by policy")
        return self._run(["/bin/sh", "-c", command])

    def _run(self, cmd: list[str], timeout: int = 300) -> dict[str, Any]:
        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(self.workspace) + os.pathsep + env.get("PYTHONPATH", "")
        try:
            proc = subprocess.run(cmd, cwd=self.workspace, text=True, capture_output=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "cmd": cmd, "returncode": -1, "stdout": "", "stderr": f"timed out after {timeout}s"}
        return {"ok": proc.returncode == 0, "cmd": cmd, "returncode": proc.returncode,
                "stdout": proc.stdout[-8000:], "stderr": proc.stderr[-8000:]}


def _check_syntax(path: str, content: str) -> None:
    if path.endswith(".py"):
        try:
            ast.parse(content)
        except SyntaxError as exc:
            raise ToolError(f"edit rejected, file would not parse: line {exc.lineno}: {exc.msg}") from exc


def workspace_diff(root: Path) -> str:
    """Diff of tracked + new files against HEAD, excluding harness state."""
    subprocess.run(["git", "add", "-A", "-N", "--", ".", ":!harness"], cwd=root, capture_output=True)
    proc = subprocess.run(["git", "diff", "--", ".", ":!harness"], cwd=root, text=True, capture_output=True)
    return proc.stdout


def parse_test_counts(output: str) -> tuple[int, int]:
    """Return (tests_run, tests_failed) from pytest or unittest output."""
    m = re.search(r"Ran (\d+) tests?", output)
    if m:
        run = int(m.group(1))
        fm = re.search(r"FAILED \(([^)]*)\)", output)
        failed = sum(int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", fm.group(1))) if fm else 0
        return run, failed
    counts = {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|errors?|skipped)", output)}
    failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    return counts.get("passed", 0) + failed, failed


def _dangerous(command: str) -> bool:
    banned = ("rm -rf /", "sudo ", "mkfs", "dd if=", "shutdown", "reboot", "| sh", "| bash", "git push", "git reset --hard")
    c = command.lower()
    return any(b in c for b in banned)


def detect_test_cmd(workspace: Path, target: str = "") -> list[str]:
    py = sys.executable
    if (workspace / "package.json").exists() and not list(workspace.glob("**/test_*.py")):
        return ["npm", "test", "--silent"]
    if importlib.util.find_spec("pytest") is not None:
        cmd = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header", "-rN"]
        if target:
            return cmd + [target]
        return cmd + (["tests"] if (workspace / "tests").is_dir() else [])
    if target:
        mod = target.split("::")[0].removesuffix(".py").replace("/", ".")
        return [py, "-m", "unittest", mod]
    if (workspace / "tests").is_dir():
        return [py, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
    return [py, "-m", "unittest", "discover"]


def _apply_simple_patch(root: Path, diff: str) -> list[str]:
    """Support *** ADD path / *** REPLACE path blocks for weak models."""
    files: list[str] = []
    for block in diff.split("*** ADD ")[1:]:
        header, _, body = block.partition("\n")
        path = header.strip()
        dest = (root / path).resolve()
        if not dest.is_relative_to(root.resolve()):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        files.append(path)
    for block in diff.split("*** REPLACE ")[1:]:
        header, _, rest = block.partition("\n")
        path = header.strip()
        dest = (root / path).resolve()
        if not dest.is_relative_to(root.resolve()):
            continue
        if "\n*** WITH\n" in rest:
            old, _, new = rest.partition("\n*** WITH\n")
            if dest.exists():
                dest.write_text(dest.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
                files.append(path)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(rest, encoding="utf-8")
            files.append(path)
    return files


def charge_full_test(task: TaskState) -> None:
    task.budget.full_test_runs += 1
