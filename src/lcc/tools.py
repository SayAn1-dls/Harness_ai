from __future__ import annotations

import ast
import difflib
import fnmatch
import functools
import importlib.util
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from lcc.sandbox import child_env, docker_enabled, docker_prefix
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
    return {"type": type_, **({"description": desc} if desc else {}), **extra}


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
    def __init__(self, workspace: Path, policy: ToolPolicy, budget: Budget, index: RepoIndex | None = None,
                 test_timeout: int = 900) -> None:
        self.workspace = Path(workspace).resolve()
        self.test_timeout = test_timeout
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
        if name in {"search_code", "find_symbol", "find_references", "git_history"}:
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

    def _iter_files(self, glob: str = ""):
        """Tracked + untracked files that git does not ignore; a plain walk when git is unavailable."""
        proc = subprocess.run(["git", "ls-files", "-z", "-co", "--exclude-standard"], cwd=self.workspace,
                              capture_output=True)
        if proc.returncode == 0 and proc.stdout:
            rels = sorted({r for r in proc.stdout.decode("utf-8", "replace").split("\0") if r})
            paths = (self.workspace / r for r in rels)
        else:
            paths = (p for p in sorted(self.workspace.rglob("*")))
        for p in paths:
            if not p.is_file():
                continue
            rel_parts = p.relative_to(self.workspace).parts
            if any(part in SKIP_DIRS for part in rel_parts[:-1]) or p.suffix in {".pyc"}:
                continue
            if glob and not _glob_match(p.relative_to(self.workspace).as_posix(), glob):
                continue
            yield p

    def _register_defaults(self) -> None:
        path = _p("string", "")  # self-explanatory; every schema byte is re-sent on every step
        R = self._register
        R(ToolSpec("repo_tree", "List repository files.", self.repo_tree,
                   _schema([], max_entries=_p("integer", ""))))
        R(ToolSpec("get_repo_map", "Ranked map of important files and their symbols. Cheap orientation.", self.get_repo_map))
        R(ToolSpec("search_code", "Case-insensitive text (or regex) search; returns path:line: text.", self.search_code,
                   _schema(["query"], query=_p("string", ""), regex=_p("boolean", ""),
                           path_glob=_p("string", "e.g. *.py"), max_hits=_p("integer", "")), cost=2))
        R(ToolSpec("find_symbol", "Where a function/class is defined.", self.find_symbol,
                   _schema(["name"], name=_p("string", ""))))
        R(ToolSpec("find_references", "Whole-word usages of an identifier.", self.find_references,
                   _schema(["symbol"], symbol=_p("string", ""))))
        R(ToolSpec("read_file", f"Numbered lines of a file (max {READ_WINDOW} per call).", self.read_file,
                   _schema(["path"], path=path, start=_p("integer", ""), end=_p("integer", "")), cost=2))
        R(ToolSpec("edit_file", "Replace old_str (exact file text, unique unless replace_all) with new_str. "
                   "Syntax-checked; returns the new lines.", self.edit_file,
                   _schema(["path", "old_str", "new_str"], path=path, old_str=_p("string", ""), new_str=_p("string", ""),
                           replace_all=_p("boolean", "")), risk="high", cost=3))
        R(ToolSpec("write_file", "Create a new file (or overwrite a small one).", self.write_file,
                   _schema(["path", "content"], path=path, content=_p("string", "")), risk="high", cost=3))
        R(ToolSpec("apply_patch", "Apply a unified diff (git apply).", self.apply_patch,
                   _schema(["diff"], diff=_p("string", "Unified diff")), risk="high", cost=4))
        R(ToolSpec("git_diff", "Show the current uncommitted diff of the task branch.", self.git_diff))
        R(ToolSpec("git_history", "Recent commits of a file, or git blame of lines start..end (find regressions).",
                   self.git_history, _schema(["path"], path=path, start=_p("integer", ""), end=_p("integer", ""))))
        R(ToolSpec("run_test", "Run the tests; pass target (test file or node id) for a focused run.", self.run_test,
                   _schema([], target=_p("string", "")), risk="medium", cost=8))
        R(ToolSpec("run_lint", "Run the configured linter.", self.run_lint, cost=4))
        R(ToolSpec("run_typecheck", "Run the configured typechecker.", self.run_typecheck, cost=4))
        R(ToolSpec("shell", "Run a sh command in the repo root (python = project interpreter). Git-state and destructive "
                   "commands are blocked.", self.shell,
                   _schema(["command"], command=_p("string", ""), timeout=_p("integer", "seconds")), risk="high", cost=6))
        R(ToolSpec("finish", "Call when done.", lambda **kw: {"done": True, **kw},
                   _schema(["summary"], summary=_p("string", ""), files=_p("array", "", items={"type": "string"}))))

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

    def search_code(self, query: str, max_hits: int = 30, regex: bool = False, path_glob: str = "") -> dict[str, Any]:
        self._charge("search_code")
        self.policy.require("read_repository")
        max_hits = max(1, min(int(max_hits or 30), 100))
        if regex:
            try:
                pat = re.compile(query, re.I)
            except re.error as exc:
                raise ToolError(f"invalid regex: {exc}") from exc
            hits = self._grep(lambda line: bool(pat.search(line)), max_hits, path_glob)
        else:
            q = query.lower()
            hits = self._grep(lambda line: q in line.lower(), max_hits, path_glob)
        if not hits:
            return {"hits": [], "note": "no matches" + (f" in files matching {path_glob}" if path_glob else "")}
        return {"hits": hits}

    def _grep(self, pred: Callable[[str], bool], max_hits: int, glob: str = "") -> list[str]:
        hits: list[str] = []
        for p in self._iter_files(glob):
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
        try:
            sl = max(1, int(start or 1))
            el = min(len(lines), int(end or sl + READ_WINDOW - 1), sl + READ_WINDOW - 1)
        except (TypeError, ValueError) as exc:
            raise ToolError("start and end must be line numbers (integers)") from exc
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

    def edit_file(self, path: str, old_str: str, new_str: str, replace_all: bool = False) -> dict[str, Any]:
        file = self._before_write(path)
        if not file.is_file():
            raise ToolError(f"no such file: {path}; use write_file to create it")
        text, encoding = _read_text(file)
        if not old_str:
            raise ToolError("old_str is empty; to create or overwrite a file use write_file")
        old_str = _strip_line_numbers(old_str, text)
        note = ""
        count = text.count(old_str)
        if count == 0:
            span = _loose_span(text, old_str)
            if span is None:
                raise ToolError("old_str not found. " + _closest_hint(text, old_str))
            old_str = text[span[0]:span[1]]
            count = 1
            note = "matched ignoring trailing whitespace / line endings"
        if count > 1 and not replace_all:
            raise ToolError(f"old_str occurs {count} times; include more surrounding lines to make it unique, "
                            "or pass replace_all=true.")
        new_text = text.replace(old_str, new_str) if replace_all else text.replace(old_str, new_str, 1)
        if new_text == text:
            raise ToolError("new_str is identical to old_str; nothing changed")
        _check_syntax(path, new_text)
        file.write_text(new_text, encoding=encoding)
        self._after_write(path)
        line = text[: text.index(old_str)].count("\n") + 1
        lines = new_text.splitlines()
        lo, hi = max(1, line - 3), min(len(lines), line + new_str.count("\n") + 3)
        out = {"ok": True, "path": path, "replacements": count if replace_all else 1,
               "snippet": "\n".join(f"{i:>4}| {lines[i - 1]}" for i in range(lo, hi + 1))}
        if note:
            out["note"] = note
        return out

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
        return {"diff": workspace_diff(self.workspace) or "(no changes yet)"}

    def git_history(self, path: str, start: int | None = None, end: int | None = None) -> dict[str, Any]:
        self._charge("search_code")
        self.policy.require("read_repository")
        rel = self._rel(self._safe_path(path))
        if start:
            cmd = ["git", "blame", "--date=short", "-L", f"{int(start)},{int(end or start)}", "--", rel]
        else:
            cmd = ["git", "log", "-n", "8", "--date=short", "--format=%h %ad %an: %s", "--stat=100", "--", rel]
        proc = subprocess.run(cmd, cwd=self.workspace, text=True, capture_output=True, timeout=60)
        if proc.returncode != 0:
            raise ToolError(proc.stderr.strip()[-300:] or "git failed")
        return {"history": proc.stdout.strip()[-4000:] or "(no history)"}

    # ---- execution tools --------------------------------------------
    def run_test(self, target: str = "") -> dict[str, Any]:
        self._charge("run_test")
        self.policy.require("run_tests")
        if not target:
            self.budget.full_test_runs += 1
            if self.budget.full_test_runs > self.budget.max_full_test_runs:
                raise ToolBudgetExceeded("test-run budget exhausted")
        res = self._run(detect_test_cmd(self.workspace, target), timeout=self.test_timeout if not target else 600)
        blob = res["stdout"] + "\n" + res["stderr"]
        run, failed = parse_test_counts(blob)
        res["tests_run"] = run
        res["tests_failed"] = failed
        res["failed_ids"] = sorted(parse_failed_ids(blob))
        res["timed_out"] = res["returncode"] == -1
        res["ok"] = res["returncode"] == 0 and run > 0
        return res

    def run_lint(self, paths: list[str] | None = None) -> dict[str, Any]:
        """Syntax-level ruff errors (E9, F63, F7, F82) in `paths` (default: the whole repo). `diagnostics` is a
        list of "path:code:message" so callers can compare against the base code instead of demanding a
        lint-clean repository."""
        self._charge("run_lint")
        self.policy.require("run_tests")
        if importlib.util.find_spec("ruff") is None:
            return {"ok": True, "skipped": True, "reason": "ruff not installed", "diagnostics": []}
        targets = [p for p in (paths if paths is not None else ["."]) if p == "." or (self.workspace / p).is_file()]
        if not targets:
            return {"ok": True, "skipped": True, "reason": "no Python files to lint", "diagnostics": []}
        res = self._run([sys.executable, "-m", "ruff", "check", "--no-cache", "--isolated", "--output-format", "json",
                         "--select", "E9,F63,F7,F82", *targets])
        try:
            rows = __import__("json").loads(res["stdout"] or "[]")
        except ValueError:
            return res | {"diagnostics": []}
        diags = []
        for r in rows:
            try:
                rel = Path(r["filename"]).resolve().relative_to(self.workspace).as_posix()
            except (KeyError, ValueError):
                rel = str(r.get("filename"))
            diags.append(f"{rel}:{r.get('code')}:{r.get('message')}")
        res["diagnostics"] = diags
        res["stdout"] = "\n".join(f"{d.split(':', 2)[0]}: {d.split(':', 2)[1]} {d.split(':', 2)[2]}" for d in diags)
        res["ok"] = not diags
        return res

    def run_typecheck(self) -> dict[str, Any]:
        self.policy.require("run_tests")
        return {"ok": True, "skipped": True, "reason": "typecheck not configured"}

    def shell(self, command: str, timeout: int = 120) -> dict[str, Any]:
        self._charge("shell")
        self.policy.require("shell")
        if _dangerous(command):
            raise ToolError("command blocked by policy (destructive, privileged, network-pipe or git-state change). "
                            "The harness manages git; use git_diff / git_history to inspect.")
        return self._run(["/bin/sh", "-c", command], timeout=max(1, min(int(timeout or 120), 600)))

    def _run(self, cmd: list[str], timeout: int = 300) -> dict[str, Any]:
        # Code from the target repo runs here: no credentials in its environment, git guarded in the workspace,
        # and `python` / `pytest` resolving to the target's own environment.
        env = child_env(self.workspace, extra_path=[str(Path(test_python()).parent)])
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = str(self.workspace) + os.pathsep + env.get("PYTHONPATH", "")
        env["CI"] = "1"  # non-interactive test runners (jest/vitest watch mode off, no prompts)
        shown = cmd[2] if cmd[:2] == ["/bin/sh", "-c"] else " ".join(cmd)
        if docker_enabled():  # untrusted code: container, no network, no credentials, only the workspace mounted
            cmd = docker_prefix(self.workspace) + (["sh", "-c", cmd[2]] if cmd[:2] == ["/bin/sh", "-c"] else cmd)
        try:
            proc = subprocess.run(cmd, cwd=self.workspace, text=True, capture_output=True, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "cmd": shown, "returncode": -1, "stdout": "", "stderr": f"timed out after {timeout}s"}
        except FileNotFoundError as exc:
            return {"ok": False, "cmd": shown, "returncode": 127, "stdout": "", "stderr": f"command not found: {exc}"}
        return {"ok": proc.returncode == 0, "cmd": shown, "returncode": proc.returncode,
                "stdout": proc.stdout[-8000:], "stderr": proc.stderr[-8000:]}


def _read_text(file: Path) -> tuple[str, str]:
    """File text plus the encoding to write it back with (legacy latin-1 files must not be corrupted)."""
    data = file.read_bytes()
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("latin-1"), "latin-1"


def _glob_match(rel: str, glob: str) -> bool:
    glob = glob.strip().lstrip("./")
    if "/" not in glob:
        return fnmatch.fnmatch(Path(rel).name, glob) or fnmatch.fnmatch(rel, glob)
    return fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(rel, glob.replace("**/", ""))


LINE_PREFIX = re.compile(r"^\s*\d+\| ?", re.M)


def _strip_line_numbers(old: str, text: str) -> str:
    """Models often paste read_file output (`  12| code`) into old_str; drop the prefixes when that is the case."""
    if old in text:
        return old
    lines = old.splitlines()
    if lines and all(LINE_PREFIX.match(line) for line in lines if line.strip()):
        return LINE_PREFIX.sub("", old)
    return old


def _loose_span(text: str, old: str) -> tuple[int, int] | None:
    """Unique match of old_str's lines ignoring trailing whitespace and CRLF. Indentation must still match
    exactly: guessing indentation silently changes Python semantics."""
    want = [line.rstrip() for line in old.replace("\r\n", "\n").strip("\n").split("\n")]
    if not want or not any(want):
        return None
    lines = text.splitlines(keepends=True)
    norm = [line.rstrip() for line in lines]
    hits = [i for i in range(len(norm) - len(want) + 1) if norm[i:i + len(want)] == want]
    if len(hits) != 1:
        return None
    start = sum(len(line) for line in lines[:hits[0]])
    end = start + sum(len(line) for line in lines[hits[0]:hits[0] + len(want)])
    if lines[hits[0] + len(want) - 1].endswith("\n") and not old.endswith("\n"):
        end -= 2 if lines[hits[0] + len(want) - 1].endswith("\r\n") else 1
    return start, end


def _closest_hint(text: str, old: str) -> str:
    lines = text.splitlines()
    first = next((line.strip() for line in old.splitlines() if line.strip()), "")
    if not first or not lines:
        return "Re-read the file and copy the exact text."
    scored = [(difflib.SequenceMatcher(None, first, line.strip()).ratio(), i) for i, line in enumerate(lines)]
    ratio, idx = max(scored)
    if ratio < 0.5:
        return "Nothing similar exists in the file; re-read it (it may have changed) and copy the exact text."
    lo, hi = max(0, idx - 2), min(len(lines), idx + max(3, len(old.splitlines())) + 1)
    near = "\n".join(f"{i + 1:>4}| {lines[i]}" for i in range(lo, hi))
    return f"The closest text is near line {idx + 1}; copy it exactly (without the line-number prefix):\n{near}"


def _check_syntax(path: str, content: str) -> None:
    if path.endswith(".py"):
        try:
            ast.parse(content)
        except SyntaxError as exc:
            raise ToolError(f"edit rejected, file would not parse: line {exc.lineno}: {exc.msg}") from exc


def workspace_diff(root: Path) -> str:
    """Diff of tracked + new files against HEAD, excluding harness state."""
    subprocess.run(["git", "add", "-A", "-N", "--", ".", ":!harness", ":!.lcc"], cwd=root, capture_output=True)
    proc = subprocess.run(["git", "diff", "--", ".", ":!harness", ":!.lcc"], cwd=root, text=True, capture_output=True)
    return proc.stdout


def changed_files(root: Path) -> list[str]:
    """Tracked and new files that differ from HEAD, excluding harness state."""
    subprocess.run(["git", "add", "-A", "-N", "--", ".", ":!harness", ":!.lcc"], cwd=root, capture_output=True)
    proc = subprocess.run(["git", "diff", "--name-only", "--", ".", ":!harness", ":!.lcc"], cwd=root, text=True, capture_output=True)
    return [line for line in proc.stdout.splitlines() if line.strip()]


@contextmanager
def base_sources(root: Path, paths: list[str]):
    """Temporarily put `paths` back to their HEAD versions (new files are removed), then restore."""
    saved: dict[str, bytes | None] = {}
    try:
        for rel in paths:
            f = root / rel
            saved[rel] = f.read_bytes() if f.exists() else None
            head = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=root, capture_output=True)
            if head.returncode == 0:
                f.write_bytes(head.stdout)
            elif f.exists():
                f.unlink()
        yield
    finally:
        for rel, data in saved.items():
            f = root / rel
            if data is None:
                if f.exists():
                    f.unlink()
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(data)


def parse_test_counts(output: str) -> tuple[int, int]:
    """Return (tests_run, tests_failed) from pytest, unittest, jest/vitest, node --test, mocha, go or cargo output."""
    m = re.search(r"Ran (\d+) tests?", output)
    if m:
        run = int(m.group(1))
        fm = re.search(r"FAILED \(([^)]*)\)", output)
        failed = sum(int(n) for n in re.findall(r"(?:failures|errors)=(\d+)", fm.group(1))) if fm else 0
        return run, failed
    cargo = re.findall(r"test result: \w+\. (\d+) passed; (\d+) failed", output)
    if cargo:
        passed = sum(int(p) for p, _ in cargo)
        failed = sum(int(f) for _, f in cargo)
        return passed + failed, failed
    jest = re.search(r"Tests:\s+(.*)", output)
    if jest:
        c = {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|total)", jest.group(1))}
        return c.get("total", c.get("passed", 0) + c.get("failed", 0)), c.get("failed", 0)
    node = re.search(r"^[#ℹ] tests (\d+)", output, re.M)
    if node:
        nf = re.search(r"^[#ℹ] fail (\d+)", output, re.M)
        return int(node.group(1)), int(nf.group(1)) if nf else 0
    mocha = re.search(r"(\d+) passing", output)
    if mocha:
        mf = re.search(r"(\d+) failing", output)
        failed = int(mf.group(1)) if mf else 0
        return int(mocha.group(1)) + failed, failed
    go_ok = re.findall(r"^ok\s+\S+", output, re.M)
    go_fail = re.findall(r"^--- FAIL: ", output, re.M)
    go_pass = re.findall(r"^--- PASS: ", output, re.M)
    if go_ok or go_fail or re.search(r"^FAIL\s+\S+", output, re.M):
        run = len(go_pass) + len(go_fail) or len(go_ok) + len(re.findall(r"^FAIL\s+\S+", output, re.M))
        return run, len(go_fail) or len(re.findall(r"^FAIL\s+\S+", output, re.M))
    counts = {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|errors?|skipped)", output)}
    failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    return counts.get("passed", 0) + failed, failed


def parse_failed_ids(output: str) -> set[str]:
    """Identifiers of failing tests, so verification can compare against the baseline set instead of
    requiring a fully green suite."""
    ids: set[str] = set()
    ids.update(re.findall(r"^(?:FAILED|ERROR) (.+?)(?: - .*)?$", output, re.M))  # pytest -rfE (ids may hold spaces)
    ids.update(f"{name} ({where})" for name, where in re.findall(r"^(?:FAIL|ERROR): (\w+) \(([\w.]+)\)", output, re.M))
    ids.update(re.findall(r"^\s*--- FAIL: (\S+)", output, re.M))  # go
    ids.update(re.findall(r"^test (\S+) \.\.\. FAILED$", output, re.M))  # cargo
    ids.update(m.strip() for m in re.findall(r"^\s*(?:✕|×|✗)\s+(.+?)(?: \(\d+ ?m?s\))?$", output, re.M))  # jest/vitest
    ids.update(re.findall(r"^not ok \d+ - (.+)$", output, re.M))  # node --test / tap
    return {i for i in ids if i and not i.startswith("[")}


GIT_STATE = re.compile(
    r"\bgit\s+(?:(?:-C|-c|--git-dir|--work-tree)\s+\S+\s+|-\S+\s+)*"
    r"(?:push|reset|checkout|switch|commit|stash|clean|rebase|merge|branch|tag|restore|am|cherry-pick|revert|"
    r"worktree|remote|config)\b"
)


def _dangerous(command: str) -> bool:
    banned = ("rm -rf /", "rm -rf ~", "rm -rf *", "sudo ", "mkfs", "dd if=", "shutdown", "reboot", "| sh", "| bash",
              ":(){", "chmod -r 777 /", "> /dev/sd")
    c = command.lower()
    return any(b in c for b in banned) or bool(GIT_STATE.search(c)) or "harness/" in c and re.search(r"\b(rm|mv|>)\b", c) is not None


def test_python() -> str:
    """Interpreter for the target repo's tests: its prepared venv when there is one, else the harness's own."""
    return os.environ.get("LCC_TEST_PYTHON") or sys.executable


def _has_pytest(py: str) -> bool:
    return True if docker_enabled() else _has_pytest_cached(py)  # the sandbox venv always installs pytest


@functools.lru_cache(maxsize=8)
def _has_pytest_cached(py: str) -> bool:
    if py == sys.executable:
        return importlib.util.find_spec("pytest") is not None
    return subprocess.run([py, "-c", "import pytest"], capture_output=True, env=child_env()).returncode == 0


def _is_python_project(workspace: Path) -> bool:
    if any((workspace / m).exists() for m in ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini")):
        return True
    return any(p for p in workspace.glob("*.py")) or any(workspace.glob("tests/**/*.py")) or any(workspace.glob("test/**/*.py"))


def detect_test_cmd(workspace: Path, target: str = "") -> list[str]:
    py = test_python()
    if not _is_python_project(workspace):
        if (workspace / "go.mod").exists():
            return ["go", "test", "-count=1", target or "./..."] if not target.endswith(".go") else \
                ["go", "test", "-count=1", "./" + str(Path(target).parent)]
        if (workspace / "Cargo.toml").exists():
            return ["cargo", "test", "-q"] + ([Path(target).stem] if target else [])
        if (workspace / "package.json").exists():
            return ["npm", "test", "--silent"] + (["--", target] if target else [])
    if _has_pytest(py):
        cmd = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header", "-rfE", "--tb=short"]
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
