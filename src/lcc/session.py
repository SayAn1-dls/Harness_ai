"""Evaluation mode (`make run`): take an issue as text, a file or a GitHub URL, fix it in the target repo, report."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from lcc.config import Config, api_key, load_config
from lcc.model import BaseProvider, ProviderError, get_provider
from lcc.orchestrator import Orchestrator, create_task
from lcc.schemas import Event, TaskState, TaskStatus
from lcc.store import HarnessStore
from lcc.workspace import Workspace

GH_ISSUE = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)")
GH_REPO = re.compile(r"^(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
REPO_LINE = re.compile(r"^\s*(?:repo|repository)\s*:\s*(\S+)\s*$", re.I | re.M)
BASE_LINE = re.compile(r"^\s*(?:base[ _-]?commit|base[ _-]?sha|base|commit)\s*:\s*([0-9a-fA-F]{7,40})\s*$", re.I | re.M)
REF_SUFFIX = re.compile(r"^(?P<repo>.+?)@(?P<ref>[\w.-]+)$")
COPY_IGNORE = shutil.ignore_patterns(".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", "harness", ".mypy_cache")
SUCCESS = {TaskStatus.HUMAN_REVIEW, TaskStatus.VERIFIED, TaskStatus.PR_READY}
END_MARKERS = {"END", "EOF", "."}
PY_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt")
REQ_FILES = ("requirements.txt", "requirements-dev.txt", "requirements-test.txt", "requirements_dev.txt",
             "test-requirements.txt", "dev-requirements.txt", "requirements/test.txt", "requirements/dev.txt")


@dataclass
class Issue:
    title: str
    body: str
    repo_hint: str = ""
    base: str = ""
    number: int | None = None
    url: str = ""
    labels: list[str] = field(default_factory=list)

    @property
    def task_id(self) -> str:
        return f"GH-{self.number}" if self.number else "ISSUE-" + time.strftime("%Y%m%d-%H%M%S")


# ------------------------------------------------------------------ issue input
def fetch_github_issue(owner: str, repo: str, number: int) -> Issue:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "lcc-harness"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    base = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
    r = httpx.get(base, headers=headers, timeout=30, follow_redirects=True)
    r.raise_for_status()
    data = r.json()
    parts = [data.get("body") or ""]
    if data.get("comments"):
        c = httpx.get(base + "/comments", headers=headers, params={"per_page": 10}, timeout=30, follow_redirects=True)
        if c.status_code == 200:
            parts += [f"\n--- comment by {x['user']['login']} ---\n{x.get('body') or ''}" for x in c.json()]
    return Issue(
        title=data.get("title") or f"Issue #{number}",
        body="\n".join(parts).strip(),
        repo_hint=f"https://github.com/{owner}/{repo}",
        number=number,
        url=data.get("html_url") or "",
        labels=[x["name"] for x in data.get("labels") or []],
    )


def parse_issue(raw: str) -> Issue:
    """A GitHub issue URL, a path to a text/markdown file, or the issue text itself."""
    raw = raw.strip()
    if not raw:
        raise ValueError("empty issue")
    m = GH_ISSUE.fullmatch(raw.split()[0]) if len(raw.split()) == 1 else None
    if m:
        return fetch_github_issue(m.group(1), m.group(2), int(m.group(3)))
    if "\n" not in raw and len(raw) < 1024:
        path = Path(raw).expanduser()
        if path.is_file():
            return parse_issue(path.read_text(encoding="utf-8"))
    lines = raw.splitlines()
    title = lines[0].lstrip("# ").strip()[:200]
    hint = REPO_LINE.search(raw)
    base = BASE_LINE.search(raw)
    num = GH_ISSUE.search(raw)
    return Issue(title=title, body=raw, repo_hint=hint.group(1) if hint else "", base=base.group(1) if base else "",
                 number=int(num.group(3)) if num else None)


# ------------------------------------------------------------------ repository
def split_ref(spec: str) -> tuple[str, str]:
    """`repo@<sha|tag|branch>` -> (repo, ref). `git@host:...` SSH URLs are left alone."""
    m = REF_SUFFIX.match(spec)
    if not m or spec.startswith("git@") and m.group("repo") == "git":
        return spec, ""
    if Path(spec).expanduser().is_dir():  # a real directory whose name contains '@'
        return spec, ""
    return m.group("repo"), m.group("ref")


def resolve_repo(spec: str, workspaces: Path, console: Console) -> Path:
    """A local git repository root is used in place (work happens on an agent/* branch, the user's branch and
    uncommitted changes are restored afterwards). A plain folder, or a folder inside another repository, is
    copied into workspaces/ first so the harness never creates nested repositories or stray state there.
    URLs and owner/name are cloned."""
    local = Path(spec).expanduser()
    if local.is_dir():
        local = local.resolve()
        if (local / ".git").exists():
            return local
        dest = workspaces / f"{local.name}-{time.strftime('%Y%m%d-%H%M%S')}"
        workspaces.mkdir(parents=True, exist_ok=True)
        console.print(f"[cyan]copying[/] {local} -> {dest} (not a git repository root; the original is left untouched)")
        shutil.copytree(local, dest, ignore=COPY_IGNORE, symlinks=True)
        return dest
    if spec.startswith(("http://", "https://", "git@", "ssh://")) or GH_REPO.match(spec):
        m = GH_REPO.match(spec)
        url = spec if not m or spec.startswith(("http", "git@", "ssh")) else f"https://github.com/{m.group(1)}/{m.group(2)}"
        name = re.sub(r"[^\w.-]+", "_", url.rstrip("/").removesuffix(".git").split("/")[-1]) or "repo"
        dest = workspaces / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
        workspaces.mkdir(parents=True, exist_ok=True)
        console.print(f"[cyan]cloning[/] {url} -> {dest}")
        proc = subprocess.run(["git", "clone", "--quiet", url, str(dest)], text=True, capture_output=True, timeout=900)
        if proc.returncode != 0:
            raise RuntimeError(f"git clone failed: {proc.stderr.strip()[-500:]}")
        return dest
    raise FileNotFoundError(f"repository not found: {spec}")


def checkout_base(repo: Path, ref: str, console: Console) -> None:
    """Pin the base commit the issue is graded against (REPO=url@sha, BASE=sha, or a `Base commit:` line)."""
    if not ref:
        return
    if _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode != 0:
        _git(repo, "fetch", "--quiet", "origin", ref)
    proc = _git(repo, "checkout", "-q", "--detach", ref)
    if proc.returncode != 0:
        raise RuntimeError(f"cannot check out base commit {ref}: {proc.stderr.strip()[-300:]}")
    console.print(f"[cyan]base commit[/] {_git(repo, 'rev-parse', '--short', 'HEAD').stdout.strip()} ({ref})")


def prepare_node(repo: Path, console: Console) -> None:
    """Install a JavaScript target's dependencies so `npm test` can run."""
    if not (repo / "package.json").exists() or (repo / "node_modules").exists() or not shutil.which("npm"):
        return
    console.print("[cyan]installing npm dependencies[/]")
    cmd = ["npm", "ci"] if (repo / "package-lock.json").exists() else ["npm", "install"]
    proc = subprocess.run(cmd + ["--no-audit", "--no-fund", "--loglevel=error"], cwd=repo, text=True,
                          capture_output=True, timeout=1800)
    if proc.returncode != 0 and cmd[1] == "ci":
        subprocess.run(["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"], cwd=repo,
                       capture_output=True, timeout=1800)


def prepare_env(repo: Path, venv: Path, console: Console) -> str | None:
    """Best-effort isolated venv with the target's dependencies so its tests can import it. Returns the python path."""
    try:
        prepare_node(repo, console)
    except Exception as exc:  # noqa: BLE001 - best effort; the tests will show what is missing
        console.print(f"[yellow]npm install failed: {exc}[/]")
    if not any((repo / m).exists() for m in PY_MARKERS):
        return None
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.exists():
        console.print(f"[cyan]preparing test environment[/] {venv}")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)

    def pip(*args: str) -> bool:
        proc = subprocess.run([str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check", *args],
                              cwd=repo, text=True, capture_output=True, timeout=1800)
        return proc.returncode == 0

    if (repo / "pyproject.toml").exists() or (repo / "setup.py").exists():
        for extra in ("[test]", "[tests]", "[testing]", "[dev]", ""):
            if pip("-e", f".{extra}"):
                break
    for req in REQ_FILES:
        if (repo / req).is_file():
            pip("-r", req)
    pip("pytest")
    return str(py)


# ------------------------------------------------------------------ run + report
def _progress(console: Console):
    def show(e: Event) -> None:
        d = e.data
        if e.event == "MODEL_CALL":
            tools = ", ".join(d.get("tool_calls") or []) or "text"
            console.print(f"  [dim]{e.agent:<9} {tools}  (+{d.get('tokens_in', 0)}/{d.get('tokens_out', 0)} tok)[/]")
        elif e.event == "AGENT_COMPLETED":
            extra = {k: v for k, v in d.items() if k in {"score", "gate", "changed", "findings", "blocking"}}
            good = e.result in {"success", "pass", "PASS", "approve"}
            mark = "[bold green]✓" if good else "[bold yellow]✗"
            console.print(f"{mark} {e.agent}[/] {e.result} {extra if extra else ''}")
        elif e.event in {"RECOVERY", "BASELINE", "DECISION", "TASK_ESCALATED", "TASK_STOPPED", "TASK_VERIFIED", "TASK_FAILED", "CONTEXT_GATE_FAILED"}:
            console.print(f"[bold magenta]» {e.event}[/] {e.result or ''} {json.dumps(d, default=str)[:300]}")
    return show


def solve(issue: Issue, repo: Path, cfg: Config, provider: BaseProvider, console: Console) -> dict:
    task_id = issue.task_id
    test_py = None
    if cfg.run.prepare_env:
        try:
            test_py = prepare_env(repo, cfg.resolve(cfg.run.workspaces_dir) / f".venv-{repo.name}", console)
        except Exception as exc:  # fall back to the harness interpreter
            console.print(f"[yellow]environment preparation failed ({exc}); using the harness python[/]")
    if test_py:
        os.environ["LCC_TEST_PYTHON"] = test_py
    else:
        os.environ.pop("LCC_TEST_PYTHON", None)

    store = HarnessStore(repo)
    origin, stashed = _prepare_repo(repo, console)
    if issue.base:
        try:
            checkout_base(repo, issue.base, console)
        except Exception:
            _restore_repo(repo, TaskState(task_id="x", repository="", workspace=str(repo), objective=""), origin, stashed, console)
            raise
    task_id = _fresh_task_id(repo, task_id)
    body = issue.body if not issue.url else f"{issue.url}\n\n{issue.body}"
    task = create_task(
        store, task_id, issue.title, repo, issue_body=body, repository=issue.repo_hint or str(repo),
        budget_overrides={"max_iterations": cfg.run.max_iterations, "tokens": cfg.run.token_budget},
    )
    store.on_event = _progress(console)
    console.print(Panel.fit(f"[bold]{task_id}[/]  {issue.title}\nrepo: {repo}\nmodel: {provider.name}/{provider.model}", title="LCC"))
    t0 = time.monotonic()
    try:
        try:
            task = Orchestrator(store, provider, coder_max_steps=cfg.run.coder_max_steps,
                                test_timeout=cfg.run.test_timeout).run(task)
        except ProviderError as exc:
            console.print(f"[red]model error:[/] {exc}")
            task = store.load_task() or task
        except Exception as exc:
            console.print(f"[red]harness error:[/] {type(exc).__name__}: {exc}")
            task = store.load_task() or task
        return report(task, store, cfg, console, round(time.monotonic() - t0, 1))
    finally:
        _restore_repo(repo, task, origin, stashed, console)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)


def _prepare_repo(repo: Path, console: Console) -> tuple[str, bool]:
    """Keep harness state out of the target's git status, remember the branch to return to, and stash the
    user's uncommitted changes so they are neither committed into the fix nor lost to a rollback."""
    Workspace(repo).ensure_git()
    exclude = repo / ".git" / "info" / "exclude"
    if exclude.parent.is_dir():
        text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if "/harness/" not in text.split():
            exclude.write_text(text + ("" if text.endswith("\n") or not text else "\n") + "/harness/\n", encoding="utf-8")
    origin = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    stashed = False
    if _git(repo, "status", "--porcelain", "--", ".", ":!harness").stdout.strip():
        proc = _git(repo, "-c", "user.name=lcc", "-c", "user.email=lcc@local",
                    "stash", "push", "-u", "-q", "-m", "lcc: uncommitted changes before run", "--", ".", ":!harness")
        if proc.returncode != 0:
            raise RuntimeError(f"could not stash uncommitted changes: {proc.stderr.strip()[-300:]}")
        stashed = True
        console.print("[yellow]uncommitted changes stashed; they are restored after the run[/]")
    return origin, stashed


def _fresh_task_id(repo: Path, task_id: str) -> str:
    """A re-run of the same issue gets its own branch so it starts from the original branch, not the last attempt."""
    candidate, n = task_id, 1
    while _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/agent/{candidate}").returncode == 0:
        n += 1
        candidate = f"{task_id}-{n}"
    return candidate


def _restore_repo(repo: Path, task: TaskState, origin: str, stashed: bool, console: Console) -> None:
    """Park any unverified work as a commit on the task branch, return to the original branch, restore stashed changes."""
    if origin and origin != "HEAD" and _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != origin:
        if task.branch.startswith("agent/") and _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == task.branch \
                and _git(repo, "status", "--porcelain", "--", ".", ":!harness").stdout.strip():
            _git(repo, "add", "-A", "--", ".", ":!harness")
            _git(repo, "-c", "user.name=lcc", "-c", "user.email=lcc@local", "commit", "-q", "-m", f"lcc: unverified attempt for {task.task_id}")
        _git(repo, "checkout", "-q", origin)
    if stashed:
        proc = _git(repo, "stash", "pop", "-q")
        if proc.returncode != 0:
            console.print(f"[red]could not restore your uncommitted changes automatically; they are kept in `git stash list`[/] "
                          f"({proc.stderr.strip()[-200:]})")


def report(task: TaskState, store: HarnessStore, cfg: Config, console: Console, elapsed: float) -> dict:
    repo = Path(task.workspace)
    diff = subprocess.run(["git", "diff", f"{task.base_commit}..HEAD", "--", ".", ":!harness"],
                          cwd=repo, text=True, capture_output=True).stdout
    if not diff:  # unverified work stays uncommitted; still surface it
        diff = subprocess.run(["git", "diff", "HEAD", "--", ".", ":!harness"], cwd=repo, text=True, capture_output=True).stdout
    out_dir = cfg.resolve(cfg.run.outputs_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    patch = out_dir / f"{task.task_id}.patch"
    patch.write_text(diff, encoding="utf-8")
    b = task.budget
    ok = task.status in SUCCESS
    summary = {
        "task_id": task.task_id,
        "resolved": ok,
        "status": task.status.value,
        "stop_reason": task.stop_reason,
        "branch": task.branch,
        "workspace": str(repo),
        "iterations": task.iteration,
        "score": task.global_score,
        "tokens": b.tokens_used,
        "tokens_in": b.tokens_in,
        "tokens_out": b.tokens_out,
        "tokens_cached": b.tokens_cached,
        "model_calls": b.model_calls,
        "tool_calls": b.tool_calls,
        "runtime_s": elapsed,
        "patch": str(patch),
        "handoff": str(store.artifacts / "HANDOFF.md"),
        "last_failure": (task.last_failure or "")[-1500:] if not ok else "",
    }
    (out_dir / f"{task.task_id}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if diff:
        console.print(Syntax(diff[:12000], "diff", theme="ansi_dark", word_wrap=True))
    table = Table(title="Result", show_header=False)
    for k in ("status", "stop_reason", "branch", "iterations", "score", "tokens", "model_calls", "tool_calls", "runtime_s", "patch", "handoff"):
        table.add_row(k, str(summary[k]))
    console.print(table)
    if task.status == TaskStatus.ESCALATED:
        console.print("[yellow]Escalated to a human: the issue is ambiguous or the context is insufficient. See the handoff.[/]")
    elif ok:
        console.print("[bold green]VERIFIED[/] fix committed on the task branch. Merge is left to a human.")
    else:
        console.print(f"[bold red]NOT VERIFIED[/] ({task.stop_reason}). Last failure:\n{summary['last_failure'][-600:]}")
    return summary


# ------------------------------------------------------------------ interactive loop
def _read_multiline(console: Console) -> str:
    console.print("[bold]Issue[/] — paste a GitHub issue URL, a file path, or the issue text. "
                  "Finish multi-line text with a line containing only [bold]END[/] (or Ctrl-D).")
    lines: list[str] = []
    while True:
        try:
            line = input("issue> " if not lines else "...    ")
        except EOFError:
            break
        if line.strip() in END_MARKERS and lines:
            break
        if not lines and line.strip().lower() in {"q", "quit", "exit"}:
            return "quit"
        lines.append(line)
        if len(lines) == 1 and (GH_ISSUE.fullmatch(line.strip()) or (line.strip() and Path(line.strip()).expanduser().is_file())):
            break
    return "\n".join(lines).strip()


def _banner(console: Console, cfg: Config, provider: BaseProvider | None, err: str) -> None:
    key = "set" if api_key() else "[red]missing[/]"
    model = f"{provider.name} / {provider.model}" if provider else f"[red]{err}[/]"
    console.print(Panel(
        f"[bold]LCC — autonomous coding-agent harness[/]\n"
        f"model: {model}   temperature={cfg.model.temperature}   AI_API_KEY: {key}\n"
        f"config: {cfg.path or 'built-in defaults'}\n"
        "Issue → Context → Plan → Code → Test → Recover → Verified patch",
        title="evaluation mode", border_style="cyan",
    ))


def start(issue_arg: str | None, repo_arg: str | None, provider_name: str | None, once: bool,
          base_arg: str | None = None) -> int:
    console = Console()
    cfg = load_config()
    provider: BaseProvider | None = None
    err = ""
    try:
        provider = get_provider(provider_name)
        if cfg.run.preflight:
            provider.preflight()
    except ProviderError as exc:
        err = str(exc)
        provider = None
    _banner(console, cfg, provider, err)
    if provider is None:
        console.print("[red]Cannot start: " + err + "[/]\nCheck AI_API_KEY (and LCC_PROVIDER / LCC_MODEL if set), then re-run `make run`.")
        return 2

    interactive = sys.stdin.isatty() and not issue_arg
    if not issue_arg and not sys.stdin.isatty():
        issue_arg = sys.stdin.read()  # piped: `make run < issue.md`
        once = True
        if not issue_arg.strip():
            console.print("[red]No issue given.[/] Pass ISSUE=<url|file|text>, pipe the issue on stdin, or run interactively.")
            return 2
    status = 0
    while True:
        raw = issue_arg if issue_arg else _read_multiline(console)
        if not raw or raw == "quit":
            return status
        try:
            issue = parse_issue(raw)
        except Exception as exc:
            console.print(f"[red]could not read issue:[/] {exc}")
            if not interactive:
                return 2
            issue_arg = None
            continue
        spec = repo_arg or issue.repo_hint
        spec, ref = split_ref(spec) if spec else (spec, "")
        issue.base = base_arg or ref or issue.base
        if not spec and interactive:
            spec = input("repository (local path, git URL, or owner/name)> ").strip()
        if not spec:
            console.print("[red]No repository given.[/] Pass REPO=<path|url> or add a 'Repository: <url>' line to the issue.")
            return 2
        try:
            repo = resolve_repo(spec, cfg.resolve(cfg.run.workspaces_dir), console)
        except Exception as exc:
            console.print(f"[red]{exc}[/]")
            if not interactive:
                return 2
            issue_arg = None
            continue
        try:
            summary = solve(issue, repo, cfg, provider, console)
        except Exception as exc:  # repository setup failed before the harness could run
            console.print(f"[red]could not run on {repo}:[/] {exc}")
            if not interactive:
                return 2
            issue_arg, repo_arg = None, None
            continue
        status = 0 if summary["resolved"] or summary["status"] == TaskStatus.ESCALATED.value else 1
        if once or not interactive:
            return status
        issue_arg, repo_arg = None, None
        console.rule("next issue (q to quit)")
