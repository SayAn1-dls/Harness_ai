"""Evaluation mode (`make run`): take an issue as text, a file or a GitHub URL, fix it in the target repo, report."""

from __future__ import annotations

import contextlib
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
from lcc.sandbox import child_env
from lcc.store import HarnessStore
from lcc.workspace import Workspace

GH_ISSUE = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)")
GH_REPO = re.compile(r"^(?:https?://github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
REPO_LINE = re.compile(r"^\s*(?:repo|repository)\s*:\s*(\S+)\s*$", re.I | re.M)
BASE_LINE = re.compile(r"^\s*(?:base[ _-]?commit|base[ _-]?sha|base|commit)\s*:\s*([0-9a-fA-F]{7,40})\s*$", re.I | re.M)
REF_SUFFIX = re.compile(r"^(?P<repo>.+?)@(?P<ref>[\w.-]+)$")
GIT_EXCLUDES = ("/harness/", "/.lcc/", "*.egg-info/", "__pycache__/", ".pytest_cache/", "*.pyc", "node_modules/")
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
    id: str = ""
    kind: str = "bug"

    @property
    def task_id(self) -> str:
        if self.id:
            return self.id
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
    body_text = "\n".join(parts).strip()
    return Issue(
        title=data.get("title") or f"Issue #{number}",
        kind=detect_kind(data.get("title") or "", body_text),
        body=body_text,
        repo_hint=f"https://github.com/{owner}/{repo}",
        number=number,
        url=data.get("html_url") or "",
        labels=[x["name"] for x in data.get("labels") or []],
    )


OPTIMIZE_WORDS = re.compile(r"\b(optimi[sz]e|optimi[sz]ation|speed ?up|faster|performance|too slow|slow(?:er|ness)?|"
                            r"inefficient|quadratic|o\(n\^?2\)|reduce (?:time|latency|memory))\b", re.I)
BUG_WORDS = re.compile(r"\b(error|exception|crash(?:es|ed)?|traceback|wrong|incorrect|fails?|failing|broken|bug)\b", re.I)


def detect_kind(title: str, body: str) -> str:
    """"optimize" for a speed request (verified by a measured speed-up), else "bug" (verified by a failing test)."""
    head = f"{title}\n{body[:600]}"
    return "optimize" if OPTIMIZE_WORDS.search(head) and not BUG_WORDS.search(title) else "bug"


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
                 number=int(num.group(3)) if num else None, kind=detect_kind(title, raw))


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
    env = child_env(repo)  # install scripts are code from the repository: no credentials
    proc = subprocess.run(cmd + ["--no-audit", "--no-fund", "--loglevel=error"], cwd=repo, text=True,
                          capture_output=True, timeout=1800, env=env)
    if proc.returncode != 0 and cmd[1] == "ci":
        subprocess.run(["npm", "install", "--no-audit", "--no-fund", "--loglevel=error"], cwd=repo,
                       capture_output=True, timeout=1800, env=env)


TEST_EXTRAS = ("test", "tests", "testing", "dev")


def declared_test_extras(repo: Path) -> tuple[list[str], list[str]]:
    """Test-related optional-dependency extras and dependency groups the project declares."""
    import configparser
    import tomllib

    extras: set[str] = set()
    groups: set[str] = set()
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError):
            data = {}
        extras |= set((data.get("project") or {}).get("optional-dependencies") or {})
        extras |= set(((data.get("tool") or {}).get("poetry") or {}).get("extras") or {})
        groups |= set(data.get("dependency-groups") or {})
    cfg = repo / "setup.cfg"
    if cfg.is_file():
        parser = configparser.ConfigParser()
        try:
            parser.read(cfg, encoding="utf-8")
            if parser.has_section("options.extras_require"):
                extras |= set(parser.options("options.extras_require"))
        except configparser.Error:
            pass
    setup_py = repo / "setup.py"
    if setup_py.is_file():
        text = setup_py.read_text(encoding="utf-8", errors="replace")
        extras |= {e for e in TEST_EXTRAS if re.search(rf"['\"]{e}['\"]\s*:", text)}
    return [e for e in TEST_EXTRAS if e in extras], [g for g in TEST_EXTRAS if g in groups]


def prepare_env(repo: Path, venv: Path, console: Console) -> str | None:
    """Best-effort isolated venv with the target's dependencies so its tests can import it. Returns the python path."""
    try:
        prepare_node(repo, console)
    except Exception as exc:  # noqa: BLE001 - best effort; the tests will show what is missing
        console.print(f"[yellow]npm install failed: {exc}[/]")
    if not any((repo / m).exists() for m in PY_MARKERS):
        return None
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = venv / ".lcc-prepared"
    if py.exists() and marker.exists():  # one install per target, not one per issue
        return str(py)
    if not py.exists():
        console.print(f"[cyan]preparing test environment[/] {venv}")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)

    def pip(*args: str) -> bool:
        proc = subprocess.run([str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check", *args],
                              cwd=repo, text=True, capture_output=True, timeout=1800, env=child_env(repo))
        return proc.returncode == 0

    if (repo / "pyproject.toml").exists() or (repo / "setup.py").exists() or (repo / "setup.cfg").exists():
        # pip exits 0 for an extra the project does not declare, so pick from the declared ones.
        extras, groups = declared_test_extras(repo)
        if not (extras and pip("-e", f".[{','.join(extras)}]")):
            pip("-e", ".")
        for group in groups:  # PEP 735 dependency groups (pip >= 25.1); ignored by older pips
            pip("--group", group)
    for req in REQ_FILES:
        if (repo / req).is_file():
            pip("-r", req)
    pip("pytest")
    marker.write_text("ok\n", encoding="utf-8")
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
        elif e.event == "TEST_STRENGTH":
            console.print(f"[bold cyan]» test strength[/] {e.result} deliberate breaks of the fix caught by its test")
        elif e.event in {"RECOVERY", "BASELINE", "DECISION", "TASK_ESCALATED", "TASK_STOPPED", "TASK_VERIFIED", "TASK_FAILED", "CONTEXT_GATE_FAILED"}:
            console.print(f"[bold magenta]» {e.event}[/] {e.result or ''} {json.dumps(d, default=str)[:300]}")
    return show


RUN_ENV = ("LCC_SANDBOX", "LCC_SANDBOX_VOLUME", "LCC_TEST_PYTHON")


@contextlib.contextmanager
def _env_scope():
    """setup_env() configures the process for one target; put it back afterwards so nothing leaks into the next
    issue (or the next test)."""
    saved = {k: os.environ.get(k) for k in RUN_ENV}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _docker_running() -> bool:
    return bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def setup_env(repo: Path, cfg: Config, console: Console, untrusted: bool = False) -> None:
    """Where the target's code runs: a Docker sandbox, or an isolated venv on this machine (or the harness python).
    sandbox="auto": Docker for untrusted code (auto mode on someone's repository) when Docker is running."""
    test_py = None
    os.environ.pop("LCC_SANDBOX", None)
    mode = cfg.run.sandbox
    if mode == "auto":
        mode = "docker" if untrusted and _docker_running() else "none"
        if untrusted and mode == "none":
            console.print("[yellow]Docker is not running: this repository's code runs on your machine (credentials "
                          "stripped, git guarded, but files readable). Start Docker for full isolation.[/]")
    if mode == "docker":
        from lcc.sandbox import docker_prepare

        os.environ["LCC_SANDBOX"] = "docker"
        os.environ["LCC_SANDBOX_VOLUME"] = "lcc-venv-" + re.sub(r"[^\w.-]", "_", repo.name)
        console.print("[cyan]sandbox:[/] docker (no network, no credentials, only the workspace mounted)")
        test_py = docker_prepare(repo, declared_test_extras(repo)[0])
    elif cfg.run.prepare_env:
        try:
            test_py = prepare_env(repo, cfg.resolve(cfg.run.workspaces_dir) / f".venv-{repo.name}", console)
        except Exception as exc:  # fall back to the harness interpreter
            console.print(f"[yellow]environment preparation failed ({exc}); using the harness python[/]")
    if test_py:
        os.environ["LCC_TEST_PYTHON"] = test_py
    else:
        os.environ.pop("LCC_TEST_PYTHON", None)


def solve(issue: Issue, repo: Path, cfg: Config, provider: BaseProvider, console: Console) -> dict:
    with _env_scope():
        return _solve(issue, repo, cfg, provider, console)


def _solve(issue: Issue, repo: Path, cfg: Config, provider: BaseProvider, console: Console) -> dict:
    task_id = issue.task_id
    setup_env(repo, cfg, console)

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
        kind=issue.kind,
    )
    store.on_event = _progress(console)
    console.print(Panel.fit(f"[bold]{task_id}[/]  {issue.title}\nrepo: {repo}\nmodel: {provider.name}/{provider.model}", title="LCC"))
    t0 = time.monotonic()
    fatal = False
    try:
        try:
            task = Orchestrator(store, provider, coder_max_steps=cfg.run.coder_max_steps,
                                test_timeout=cfg.run.test_timeout).run(task)
        except ProviderError as exc:
            console.print(f"[red]model error:[/] {exc}")
            fatal = exc.fatal
            task = store.load_task() or task
        except Exception as exc:
            console.print(f"[red]harness error:[/] {type(exc).__name__}: {exc}")
            task = store.load_task() or task
        return report(task, store, cfg, console, round(time.monotonic() - t0, 1)) | {"fatal": fatal}
    finally:
        _restore_repo(repo, task, origin, stashed, console)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)


def _prepare_repo(repo: Path, console: Console) -> tuple[str, bool]:
    """Keep harness state out of the target's git status, remember the branch to return to, and stash the
    user's uncommitted changes so they are neither committed into the fix nor lost to a rollback."""
    Workspace(repo).ensure_git()
    exclude = repo / ".git" / "info" / "exclude"
    if exclude.parent.is_dir():  # harness state and build artefacts of the env setup never enter the fix commit
        text = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        missing = [p for p in GIT_EXCLUDES if p not in text.split()]
        if missing:
            exclude.write_text(text + ("" if text.endswith("\n") or not text else "\n") + "\n".join(missing) + "\n",
                               encoding="utf-8")
    origin = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    stashed = False
    if _git(repo, "status", "--porcelain", "--", ".", ":!harness", ":!.lcc").stdout.strip():
        proc = _git(repo, "-c", "user.name=lcc", "-c", "user.email=lcc@local",
                    "stash", "push", "-u", "-q", "-m", "lcc: uncommitted changes before run", "--", ".", ":!harness", ":!.lcc")
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
                and _git(repo, "status", "--porcelain", "--", ".", ":!harness", ":!.lcc").stdout.strip():
            _git(repo, "add", "-A", "--", ".", ":!harness", ":!.lcc")
            _git(repo, "-c", "user.name=lcc", "-c", "user.email=lcc@local", "commit", "-q", "-m", f"lcc: unverified attempt for {task.task_id}")
        _git(repo, "checkout", "-q", origin)
    if stashed:
        proc = _git(repo, "stash", "pop", "-q")
        if proc.returncode != 0:
            console.print(f"[red]could not restore your uncommitted changes automatically; they are kept in `git stash list`[/] "
                          f"({proc.stderr.strip()[-200:]})")


def report(task: TaskState, store: HarnessStore, cfg: Config, console: Console, elapsed: float) -> dict:
    repo = Path(task.workspace)
    diff = subprocess.run(["git", "diff", f"{task.base_commit}..HEAD", "--", ".", ":!harness", ":!.lcc"],
                          cwd=repo, text=True, capture_output=True).stdout
    if not diff:  # unverified work stays uncommitted; still surface it
        diff = subprocess.run(["git", "diff", "HEAD", "--", ".", ":!harness", ":!.lcc"], cwd=repo, text=True, capture_output=True).stdout
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
        "summary": _coder_summary(store, task),
        "verification": task.verification,
        "proof": task.verification.get("proof") or {},
    }
    if summary["proof"]:  # proof-carrying fix: replay anywhere with `lcc verify --proof <file>`
        (out_dir / f"{task.task_id}.proof.json").write_text(json.dumps(summary["proof"], indent=2), encoding="utf-8")
    (out_dir / f"{task.task_id}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if diff:
        console.print(Syntax(diff[:12000], "diff", theme="ansi_dark", word_wrap=True))
    table = Table(title="Result", show_header=False)
    for k in ("status", "stop_reason", "branch", "iterations", "score", "tokens", "model_calls", "tool_calls", "runtime_s", "patch", "handoff"):
        table.add_row(k, str(summary[k]))
    console.print(table)
    mut = task.verification.get("mutation") or {}
    if ok and mut.get("total"):
        console.print(f"test strength: {mut['killed']}/{mut['total']} deliberate breaks of the fix are caught by its test")
    if ok and summary["proof"]:
        console.print(f"proof: lcc verify --repo {repo} --proof {out_dir / (task.task_id + '.proof.json')}")
    if task.status == TaskStatus.ESCALATED:
        console.print("[yellow]Escalated to a human: the issue is ambiguous or the context is insufficient. See the handoff.[/]")
    elif ok:
        console.print("[bold green]VERIFIED[/] fix committed on the task branch. Merge is left to a human.")
    else:
        console.print(f"[bold red]NOT VERIFIED[/] ({task.stop_reason}). Last failure:\n{summary['last_failure'][-600:]}")
    return summary


def _coder_summary(store: HarnessStore, task: TaskState) -> str:
    path = store.artifacts / f"coder_{task.iteration}.json"
    try:
        return str(json.loads(path.read_text(encoding="utf-8")).get("summary") or "")
    except (OSError, ValueError):
        return ""


# ------------------------------------------------------------------ repo-only mode
def is_repo_only(raw: str) -> bool:
    """A bare repository link or local directory, with no issue text: find the work automatically."""
    raw = raw.strip()
    if not raw or len(raw.split()) != 1 or GH_ISSUE.search(raw):
        return False
    spec, _ = split_ref(raw)
    if Path(spec).expanduser().is_dir():
        return True
    if Path(raw).expanduser().is_file():  # an issue file, e.g. issues/bug-12
        return False
    return bool(re.match(r"^(?:https?://|git@|ssh://)", spec)) or bool(GH_REPO.fullmatch(spec) and "/" in spec
                                                                       and not Path(spec).suffix)


def pr_body(cand, summary: dict, head_ref: str = "", others: list[dict] | None = None) -> str:
    from lcc.proof import manual_steps

    v = summary.get("verification") or {}
    proof = summary.get("proof") or {}
    mut = v.get("mutation") or {}
    lines = [
        f"## Problem\n\n{cand.body}",
        f"## Fix\n\n{summary.get('summary') or 'See the diff.'}",
        "## Verification",
        f"- Test suite: {v.get('tests_run')} test(s) run, {v.get('tests_failed')} failing"
        + (" (all pre-existing: they failed before this change as well)" if v.get("tests_failed") else ""),
        f"- Proof level {v.get('proof_level')}/5: "
        + ("a new or changed test fails on the original code and passes with this change."
           if v.get("proof_level") == 5 else "behavior-preserving change; the existing tests pass with no regressions."),
        f"- Iterations: {summary.get('iterations')}, model calls: {summary.get('model_calls')}, tokens: {summary.get('tokens')}",
    ]
    speed = v.get("speed") or {}
    if speed.get("ok"):
        lines.append(f"- Speed: **{speed['message']}**, measured by the harness on the old and the new code")
    if mut.get("total"):
        lines.append(f"- Test strength: the new test catches **{mut['killed']}/{mut['total']}** deliberate breaks of this fix"
                     + (f" (survived: {', '.join(s['op'] for s in mut['survived'][:3])})" if mut.get("survived") else ""))
    if others:
        lines += ["", f"## Other issues found in this repository ({len(others)})", ""]
        lines += [f"- {e['status']}: {e.get('title', '')} (`{e.get('file') or '-'}`)" for e in others[:8]]
    if proof:
        lines += ["", "## Verify it yourself (no need to trust the AI)", "",
                  "```bash", manual_steps(proof, head_ref), "```"]
    lines += [
        "",
        f"Found by: {cand.source}. Generated by the LCC autonomous harness. It never merges: please review the "
        "change and merge it only if you agree.",
    ]
    return "\n\n".join(lines[:2]) + "\n\n" + "\n".join(lines[2:])


def ensure_github_login(console: Console, interactive: bool) -> str | None:
    """Pull requests need a GitHub login (a username alone cannot push code or open a PR). Use the GitHub CLI or a
    token if one is there; otherwise ask the user to sign in. The token is kept in this process only."""
    import getpass

    from lcc.github_pr import gh_login, github_identity, token_login

    login, how = github_identity()
    if login:
        console.print(f"GitHub: pull requests will be opened as [bold]@{login}[/] ({'GitHub CLI' if how == 'gh' else 'token'})")
        return login
    if not interactive:
        console.print("[yellow]No GitHub login found, so verified fixes will stay on local branches. To get pull "
                      "requests, run `gh auth login` or set GITHUB_TOKEN, then run again.[/]")
        return None
    console.print(Panel(
        "When a fix is proven, LCC opens a pull request on your GitHub account.\n"
        "GitHub needs you to sign in for that: a username alone can't push code or open a PR.\n\n"
        "  [bold]1[/]  Sign in with your browser (recommended: GitHub's own login page; LCC never sees your password)\n"
        "  [bold]2[/]  Paste a personal access token (hidden, used only for this run, never saved)\n"
        "  [bold]3[/]  Skip pull requests (fixes stay on local branches)",
        title="GitHub login", border_style="cyan"))
    choice = (input("choose 1, 2 or 3 [1]> ").strip() or "1")[:1]
    if choice == "1":
        if shutil.which("gh"):
            subprocess.run(["gh", "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web"])
            login = gh_login()
        else:
            console.print("The GitHub CLI isn't installed (https://cli.github.com, or `brew install gh`). "
                          "You can paste a token instead.")
            choice = "2"
    if choice == "2" and not login:
        console.print("Create one at https://github.com/settings/tokens (classic, scope: [bold]repo[/]), then paste it.")
        tok = getpass.getpass("GitHub token (input hidden)> ").strip()
        if tok:
            login = token_login(tok)
            if login:
                os.environ["GH_TOKEN"] = tok  # this process only: never written to disk
            else:
                console.print("[red]GitHub rejected that token.[/]")
    if login:
        console.print(f"GitHub: pull requests will be opened as [bold]@{login}[/]")
        return login
    console.print("Skipping pull requests: verified fixes will stay on local branches.")
    return None


def _pr_permission(repo: Path, cfg: Config, console: Console, wanted: bool, explicit: bool, n: int) -> bool:
    """PRs on someone else's repository are outward-facing: ask first (or require PR=1), and respect a cap."""
    from lcc.github_pr import repo_access

    if not wanted or n == 0:
        return False
    try:
        slug, can_push, mine = repo_access(repo)
    except Exception as exc:  # noqa: BLE001 - not a GitHub repo or gh missing: keep branches only
        console.print(f"[yellow]pull requests disabled: {exc}[/]")
        return False
    if mine >= cfg.auto.max_open_prs:
        console.print(f"[yellow]you already have {mine} open PR(s) on {slug} (cap {cfg.auto.max_open_prs}); "
                      "keeping the fixes as local branches[/]")
        return False
    if can_push or explicit:
        return True
    if sys.stdin.isatty():
        kind = "draft " if cfg.auto.pr_draft else ""
        answer = input(f"Open up to {n} {kind}pull request(s) on {slug} from your fork? [y/N] ").strip().lower()
        return answer in {"y", "yes"}
    console.print(f"[yellow]{slug} is not your repository: not opening PRs without PR=1 (fixes kept as branches)[/]")
    return False


def auto_fix(spec: str, cfg: Config, provider: BaseProvider, console: Console, *, open_prs: bool = True,
             base: str = "", explicit_pr: bool = False) -> dict:
    with _env_scope():
        return _auto_fix(spec, cfg, provider, console, open_prs=open_prs, base=base, explicit_pr=explicit_pr)


def _auto_fix(spec: str, cfg: Config, provider: BaseProvider, console: Console, *, open_prs: bool = True,
              base: str = "", explicit_pr: bool = False) -> dict:
    """Repo link only: clone, find problems, fix and verify each, open one PR per verified fix."""
    from lcc.discover import discover
    from lcc.github_pr import open_pull_request

    spec, ref = split_ref(spec)
    repo = resolve_repo(spec, cfg.resolve(cfg.run.workspaces_dir), console)
    if base or ref:
        checkout_base(repo, base or ref, console)
    cfg.run.sandbox = "docker" if cfg.run.sandbox == "auto" and _docker_running() else cfg.run.sandbox
    setup_env(repo, cfg, console, untrusted=True)  # discovery runs the target's tests too: same sandbox as the fixes
    console.print(Panel.fit(f"[bold]auto mode[/] {repo}\nfinding bugs, security issues and clear optimizations; "
                            f"up to {cfg.auto.max_fixes} fix(es), each verified" +
                            (", one pull request per verified fix" if open_prs else " (pull requests disabled)"),
                            title="LCC"))
    from lcc.discover import new_stats

    counter = CountingProvider(provider)
    honesty = new_stats()
    ledger: list[dict] = []
    coverage: dict = {}
    full = cfg.auto.audit_scope == "full"
    found = discover(repo, counter, max_candidates=cfg.auto.max_fixes, audit_calls=cfg.auto.audit_calls,
                     audit_chars=cfg.auto.audit_chars, log=lambda m: console.print(f"[cyan]{m}[/]"), stats=honesty,
                     audit_budget=cfg.auto.audit_budget_chars if full and cfg.auto.audit_calls > 0 else None,
                     ledger=ledger, coverage=coverage)
    if coverage:
        console.print(f"[cyan]read {coverage['files_read']}/{coverage['source_files']} source files "
                      f"({coverage['lines_read']}/{coverage['source_lines']} lines)[/]")
    table = Table(title="Candidates", show_lines=False)
    for col in ("#", "source", "kind", "title"):
        table.add_column(col)
    for i, c in enumerate(found, 1):
        table.add_row(str(i), c.source, c.kind, c.title)
    console.print(table if found else "[green]No provable problems found.[/] Nothing to fix.")

    results = []
    open_prs = _pr_permission(repo, cfg, console, open_prs, explicit_pr, len(found)) if found else False
    for i, cand in enumerate(found, 1):
        slug = re.sub(r"[^a-z0-9]+", "-", cand.title.lower()).strip("-")[:40]
        issue = Issue(title=cand.title, body=cand.issue_text(), id=f"AUTO-{i}-{slug}",
                      kind="optimize" if cand.kind == "performance" else "bug")
        console.rule(f"[{i}/{len(found)}] {cand.title}")
        summary = solve(issue, repo, cfg, provider, console)
        row = {"candidate": cand.title, "source": cand.source, "kind": cand.kind, "resolved": summary["resolved"],
               "status": summary["status"], "branch": summary["branch"], "patch": summary["patch"],
               "tokens": summary["tokens"], "pr": ""}
        if summary["resolved"] and open_prs:
            try:
                others = [e for e in ledger if e.get("candidate") != cand.title and not e["status"].startswith("dropped")]
                row["pr"] = open_pull_request(repo, summary["branch"], _pr_title(cand),
                                              pr_body(cand, summary, f"lcc/{issue.task_id.lower()}", others),
                                              draft=cfg.auto.pr_draft, remote_branch=f"lcc/{issue.task_id.lower()}")
                console.print(f"[bold green]pull request:[/] {row['pr']}")
            except Exception as exc:  # noqa: BLE001 - keep going; the verified branch is still there
                row["pr_error"] = str(exc)
                console.print(f"[yellow]could not open the pull request:[/] {exc}")
        results.append(row)
        for entry in ledger:
            if entry.get("candidate") == cand.title:
                speed = (summary.get("verification") or {}).get("speed") or {}
                entry["status"] = ("fixed and proven" + (f" ({speed['message']})" if speed.get("ok") else "")
                                   if summary["resolved"] else f"attempted, not proven ({summary['status']})")
                entry["branch"] = summary["branch"]
        if cand.source == "audit" and not summary.get("fatal"):
            honesty["attempted"] += 1
            honesty["proven" if summary["resolved"] else "unproven"] += 1
        if summary.get("fatal"):  # bad key / no balance: every remaining candidate would fail the same way
            console.print("[red]stopping: the model account error affects every remaining fix[/]")
            break

    honesty["rate"] = round(honesty["proven"] / honesty["attempted"], 2) if honesty["attempted"] else None
    if honesty["claimed"]:
        console.print(Panel(honesty_text(honesty), title="AI honesty report", border_style="yellow"))
    for r in results:  # PR links into the ledger
        for entry in ledger:
            if entry.get("candidate") == r["candidate"] and r.get("pr"):
                entry["pr"] = r["pr"]
    report_md = issue_report(repo, ledger, coverage, honesty)
    out_dir0 = cfg.resolve(cfg.run.outputs_dir)
    out_dir0.mkdir(parents=True, exist_ok=True)
    (out_dir0 / f"ISSUES-{repo.name}.md").write_text(report_md, encoding="utf-8")
    console.print(f"[bold]issue report:[/] {out_dir0 / f'ISSUES-{repo.name}.md'} ({len(ledger)} issue(s))")
    out = {"repo": str(repo), "candidates": len(found), "fixed": sum(r["resolved"] for r in results),
           "ai_honesty": honesty, "issues": ledger, "coverage": coverage,
           "discovery_tokens": counter.tokens, "total_tokens": counter.tokens + sum(r["tokens"] for r in results),
           "prs": [r["pr"] for r in results if r["pr"]], "results": results}
    out_dir = cfg.resolve(cfg.run.outputs_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"auto-{repo.name}.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    final = Table(title="Auto mode result")
    for col in ("candidate", "verified", "pull request / branch"):
        final.add_column(col)
    for r in results:
        final.add_row(r["candidate"][:60], "yes" if r["resolved"] else f"no ({r['status']})",
                      r["pr"] or r.get("pr_error", "")[:60] or r["branch"])
    console.print(final)
    return out


class CountingProvider(BaseProvider):
    """Counts the tokens of calls made outside a task (repo discovery)."""

    def __init__(self, inner: BaseProvider) -> None:
        self.inner, self.name, self.model, self.tokens = inner, inner.name, inner.model, 0

    def chat(self, messages, *, tools=None, max_tokens=4096, agent="", **kw):
        res = self.inner.chat(messages, tools=tools, max_tokens=max_tokens, agent=agent, **kw)
        self.tokens += res.usage.total
        return res


def issue_report(repo: Path, ledger: list[dict], coverage: dict, honesty: dict) -> str:
    """Every issue found, fixable or not, with where it came from and what happened to it."""
    icon = {"fixed": "✅", "attempted": "⚠️", "queued": "⏳", "found": "📝", "dropped": "🗑️"}
    lines = [f"# Issues found in `{repo.name}`", ""]
    if coverage:
        lines.append(f"The AI read **{coverage['files_read']} of {coverage['source_files']}** source files "
                     f"({coverage['lines_read']:,} of {coverage['source_lines']:,} lines). The test suite and the "
                     "static defect checker covered the whole repository.")
        if coverage.get("unread"):
            lines.append(f"Not read by the AI (budget): {', '.join(coverage['unread'][:15])}"
                         + (" …" if len(coverage["unread"]) > 15 else ""))
        lines.append("")
    order = {"fixed": 0, "attempted": 1, "queued": 2, "found": 3, "dropped": 4}

    def key(e: dict) -> str:
        m = re.match(r"[a-z]+", e["status"])
        return m.group(0) if m else "other"

    counts: dict[str, int] = {}
    for e in ledger:
        counts[key(e)] = counts.get(key(e), 0) + 1
    lines += [" · ".join(f"{icon.get(k, '•')} {v} {k}" for k, v in sorted(counts.items(), key=lambda kv: order.get(kv[0], 9))), "",
              "| # | Status | Kind | Where | Issue | Found by |", "|---|---|---|---|---|---|"]
    for i, e in enumerate(sorted(ledger, key=lambda e: order.get(key(e), 9)), 1):
        where = f"`{e.get('file') or '-'}`" + (f":{e['line']}" if e.get("line") else "")
        status = e["status"] + (f" · [PR]({e['pr']})" if e.get("pr") else "")
        title = str(e.get("title", "")).replace("|", "\\|")[:90]
        lines.append(f"| {i} | {status} | {e.get('kind', '')} | {where} | {title} | {e.get('source', '')} |")
    if honesty.get("claimed"):
        lines += ["", "## How often the AI was right", "", "```", honesty_text(honesty), "```"]
    lines += ["", "Only issues proven with a test (or, for optimizations, a measured speed-up with every test still "
              "passing) are fixed. Everything else is listed so a human can decide."]
    return "\n".join(lines) + "\n"


def honesty_text(h: dict) -> str:
    """How often the model's bug claims survived contact with a test."""
    dropped = h["dropped_no_trigger"] + h["dropped_low_confidence"] + h["dropped_unread_file"]
    lines = [f"The model claimed {h['claimed']} bug(s).",
             f"  {dropped} dropped before any work: {h['dropped_no_trigger']} had no way to trigger them, "
             f"{h['dropped_low_confidence']} low confidence, {h['dropped_unread_file']} in files it never read.",
             f"  {h['attempted']} attempted: {h['proven']} proven real with a failing test, {h['unproven']} could not be proven."]
    if h.get("not_attempted"):
        lines.append(f"  {h['not_attempted']} more were kept but not attempted (max_fixes).")
    if h["attempted"]:
        lines.append(f"Proven rate: {h['proven']}/{h['attempted']} attempted claims"
                     f" ({h['proven']}/{h['claimed']} of everything it claimed).")
    return "\n".join(lines)


def fix_and_pr(issue: Issue, repo: Path, cfg: Config, provider: BaseProvider, console: Console, *,
               open_prs: bool = True, explicit: bool = False) -> dict:
    """make run: fix one issue and, once the fix is proven, open a draft pull request on the user's GitHub repo.
    The PR targets the branch the fix was built on; `Fixes #N` closes the issue when the maintainer merges it."""
    from types import SimpleNamespace

    from lcc.github_pr import github_slug, open_pull_request

    origin_branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    summary = solve(issue, repo, cfg, provider, console)
    summary["pr"] = ""
    if not summary["resolved"] or not open_prs:
        return summary
    try:
        slug = github_slug(repo)
    except RuntimeError:
        console.print("[dim]not a GitHub repository: the verified fix stays on its local branch[/]")
        return summary
    if not _pr_permission(repo, cfg, console, True, explicit, 1):
        return summary
    closes = f"\n\nFixes #{issue.number}" if issue.number and slug.lower() in (issue.url or issue.body[:300]).lower() else ""
    cand = SimpleNamespace(body=truncate_issue(issue.body) + closes, source="the issue you reported", kind=issue.kind,
                           title=issue.title)
    try:
        summary["pr"] = open_pull_request(
            repo, summary["branch"], _pr_title(cand), pr_body(cand, summary, f"lcc/{summary['task_id'].lower()}"),
            draft=cfg.auto.pr_draft, remote_branch=f"lcc/{summary['task_id'].lower()}", base_branch=origin_branch)
        console.print(f"[bold green]pull request:[/] {summary['pr']}  (draft: review it, then merge or close it)")
    except Exception as exc:  # noqa: BLE001 - the verified fix is still on its branch
        summary["pr_error"] = str(exc)
        console.print(f"[yellow]could not open the pull request:[/] {exc}\nThe verified fix is on branch {summary['branch']}.")
    return summary


def truncate_issue(text: str, limit: int = 3000) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…"


def _pr_title(cand) -> str:
    prefix = {"security": "fix(security)", "performance": "perf", "optimize": "perf"}.get(cand.kind, "fix")
    return f"{prefix}: {cand.title[0].lower()}{cand.title[1:]}"


# ------------------------------------------------------------------ interactive loop
def _read_multiline(console: Console) -> str:
    console.print("[bold]Issue[/] — paste a GitHub issue URL, a file path, or the issue text "
                  "(finish multi-line text with a line containing only [bold]END[/] or Ctrl-D).\n"
                  "Or paste just a [bold]repository link[/]: the agent finds the bugs itself, fixes them and opens PRs.")
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
        if len(lines) == 1 and (GH_ISSUE.fullmatch(line.strip()) or is_repo_only(line)
                                or (line.strip() and Path(line.strip()).expanduser().is_file())):
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
          base_arg: str | None = None, auto: bool = False, open_prs: bool | None = None) -> int:
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

    prs = cfg.auto.open_pr if open_prs is None else open_prs
    if auto:  # `make auto REPO=...`: no issue at all
        spec = repo_arg or issue_arg
        if not spec:
            console.print("[red]auto mode needs a repository:[/] make auto REPO=<url|path>")
            return 2
        if prs and not ensure_github_login(console, sys.stdin.isatty()):
            prs = False
        out = auto_fix(spec, cfg, provider, console, open_prs=prs, base=base_arg or "", explicit_pr=bool(open_prs))
        return 0 if out["fixed"] or not out["candidates"] else 1
    interactive = sys.stdin.isatty() and not issue_arg
    if not issue_arg and not sys.stdin.isatty():
        issue_arg = sys.stdin.read()  # piped: `make run < issue.md`
        once = True
        if not issue_arg.strip():
            console.print("[red]No issue given.[/] Pass ISSUE=<url|file|text>, pipe the issue on stdin, or run interactively.")
            return 2
    if prs and not ensure_github_login(console, sys.stdin.isatty()):  # ask up front, not after the work is done
        prs = False
    status = 0
    while True:
        raw = issue_arg if issue_arg else _read_multiline(console)
        if not raw or raw == "quit":
            return status
        if is_repo_only(raw) and not repo_arg:
            try:
                out = auto_fix(raw.strip(), cfg, provider, console, open_prs=prs, base=base_arg or "",
                               explicit_pr=bool(open_prs))
                status = 0 if out["fixed"] or not out["candidates"] else 1
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]auto mode failed:[/] {exc}")
                status = 2
            if once or not interactive:
                return status
            issue_arg = None
            console.rule("next (q to quit)")
            continue
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
            summary = fix_and_pr(issue, repo, cfg, provider, console, open_prs=prs, explicit=bool(open_prs))
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
