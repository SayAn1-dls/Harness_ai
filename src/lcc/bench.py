"""Benchmark runner: copy each fixture repo, run the harness, then grade with a hidden check the agent never saw."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

from lcc.model import BaseProvider, ScriptedProvider, get_provider
from lcc.sandbox import child_env
from lcc.orchestrator import Orchestrator, create_task
from lcc.schemas import TaskStatus
from lcc.store import HarnessStore

SUCCESS = {TaskStatus.HUMAN_REVIEW, TaskStatus.VERIFIED, TaskStatus.PR_READY}
CACHE = Path(__file__).resolve().parents[2] / "benchmarks" / ".cache"


# ------------------------------------------------------------------ real-repository tasks
# task.json with "repo" + "base" + "fix": the workspace is the real repository at `base` (the parent of a real bug-fix
# commit); grading runs the test files of the `fix` commit (`hidden_tests`), which fail on `base` and pass on `fix`.
def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, timeout=900)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()[-300:]}")
    return proc


def _mirror(url: str) -> Path:
    name = re.sub(r"[^\w.-]+", "_", url.rstrip("/").removesuffix(".git").split("github.com/")[-1])
    mirror = CACHE / "repos" / name
    if not mirror.exists():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        _git(mirror.parent, "clone", "--quiet", "--no-checkout", url, str(mirror))
    return mirror


def materialize(meta: dict[str, Any], dest: Path) -> None:
    """Create the task workspace: a copy of the fixture, or the real repository checked out at its base commit."""
    if "repo" not in meta:
        shutil.copytree(meta["dir"] / "repo", dest)
        return
    mirror = _mirror(meta["repo"])
    if _git(mirror, "cat-file", "-e", f"{meta['fix']}^{{commit}}", check=False).returncode != 0:
        _git(mirror, "fetch", "--quiet", "origin")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(dest.parent, "clone", "--quiet", "--no-checkout", str(mirror), str(dest))
    _git(dest, "checkout", "--quiet", "--detach", meta["base"])
    _git(dest, "remote", "remove", "origin")  # nothing from a benchmark run can be pushed anywhere


def bench_python(tasks: list[dict[str, Any]]) -> str:
    """One isolated interpreter for the real-repository tasks (pytest + their declared test deps)."""
    deps = sorted({"pytest", *(d for t in tasks for d in t.get("deps") or [])})
    venv = CACHE / "venv"
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = venv / ".deps"
    if not py.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    if not marker.exists() or marker.read_text(encoding="utf-8") != " ".join(deps):
        subprocess.run([str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check", *deps],
                       check=True, capture_output=True, timeout=900)
        marker.write_text(" ".join(deps), encoding="utf-8")
    return str(py)


def hidden_check_real(meta: dict[str, Any], repo: Path, py: str) -> tuple[bool, str]:
    """Put the fix commit's versions of the hidden test files in place and run them."""
    for rel in meta["hidden_tests"]:
        blob = subprocess.run(["git", "show", f"{meta['fix']}:{rel}"], cwd=repo, capture_output=True, timeout=60)
        if blob.returncode != 0:
            return False, f"cannot read {rel} from the fix commit"
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_bytes(blob.stdout)
    env = child_env() | {"PYTHONPATH": str(repo), "PYTHONDONTWRITEBYTECODE": "1"}
    proc = subprocess.run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "-rfE", "--tb=short",
                           *meta["hidden_tests"]], cwd=repo, text=True, capture_output=True, env=env, timeout=600)
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]


def validate_real(tasks_dir: Path, only: list[str] | None = None) -> list[dict[str, Any]]:
    """Check every real task: its hidden tests must fail at `base` and pass at `fix`."""
    tasks = [t for t in load_tasks(tasks_dir, only) if "repo" in t]
    py = bench_python(tasks)
    rows = []
    for meta in tasks:
        work = Path(tempfile.mkdtemp(prefix="lcc-validate-")) / "repo"
        materialize(meta, work)
        fails_on_base, _ = hidden_check_real(meta, work, py)
        _git(work, "checkout", "--quiet", "--force", "--detach", meta["fix"])
        passes_on_fix, out = hidden_check_real(meta, work, py)
        rows.append({"id": meta["id"], "fails_on_base": not fails_on_base, "passes_on_fix": passes_on_fix,
                     "valid": (not fails_on_base) and passes_on_fix, "detail": "" if passes_on_fix else out[-400:]})
        shutil.rmtree(work.parent, ignore_errors=True)
    return rows


def load_tasks(tasks_dir: Path, only: list[str] | None = None) -> list[dict[str, Any]]:
    tasks = []
    for d in sorted(p for p in tasks_dir.iterdir() if (p / "task.json").exists()):
        meta = json.loads((d / "task.json").read_text(encoding="utf-8"))
        if only and meta["id"] not in only:
            continue
        meta["dir"] = d
        meta["issue"] = (d / "issue.md").read_text(encoding="utf-8")
        tasks.append(meta)
    return tasks


def oracle_provider(meta: dict[str, Any], repo: Path) -> BaseProvider:
    """NOT a model: replays the real fix commit (source + tests) through the normal coder tools. It checks the harness
    machinery on real repositories (baseline, proof, lint, review, grading), independently of model quality."""
    from lcc.model import ToolCall

    files = _git(repo, "diff", "--name-only", meta["base"], meta["fix"]).stdout.split()
    writes = []
    for rel in files:
        blob = subprocess.run(["git", "show", f"{meta['fix']}:{rel}"], cwd=repo, capture_output=True, timeout=60)
        if blob.returncode == 0 and rel.endswith(".py"):
            writes.append(ToolCall("write_file", {"path": rel, "content": blob.stdout.decode("utf-8", "replace")}))
    return ScriptedProvider({"coder": [writes, [ToolCall("finish", {"summary": "oracle: replayed the reference fix"})]]})


def _provider_for(name: str, task_dir: Path, shared: BaseProvider | None = None) -> BaseProvider:
    if name == "scripted":
        path = task_dir / "scripted.json"
        return ScriptedProvider(json.loads(path.read_text(encoding="utf-8")) if path.exists() else {})
    return shared or get_provider(name)


def hidden_check(task_dir: Path, repo: Path) -> tuple[bool, str]:
    check_src = task_dir / "check"
    if not check_src.is_dir():
        return True, "no hidden check"
    check = repo.parent / "hidden_check"
    shutil.copytree(check_src, check, dirs_exist_ok=True)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(check), str(check)],
        cwd=repo, text=True, capture_output=True, env={"PYTHONPATH": str(repo), "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=300,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]


def run_one(meta: dict[str, Any], provider_name: str, max_iterations: int, workdir: Path,
            provider: BaseProvider | None = None) -> dict[str, Any]:
    task_dir: Path = meta["dir"]
    repo = workdir / meta["id"] / "repo"
    materialize(meta, repo)
    real = "repo" in meta
    if real:
        os.environ["LCC_TEST_PYTHON"] = meta["_python"]
    else:
        os.environ.pop("LCC_TEST_PYTHON", None)
    store = HarnessStore(repo)
    task = create_task(store, meta["id"], meta["objective"], repo, issue_body=meta["issue"],
                       budget_overrides={"max_iterations": max_iterations})
    t0 = time.monotonic()
    error = ""
    fatal = False
    try:
        llm = oracle_provider(meta, repo) if provider_name == "oracle" else _provider_for(provider_name, task_dir, provider)
        task = Orchestrator(store, llm).run(task)
    except Exception as exc:  # recorded; only account-level provider errors stop the benchmark
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
        fatal = bool(getattr(exc, "fatal", False))
        task = store.load_task() or task
    elapsed = round(time.monotonic() - t0, 1)

    check_ok, check_out = (False, "")
    if meta["expect"] == "resolve":
        check_ok, check_out = hidden_check_real(meta, repo, meta["_python"]) if real else hidden_check(task_dir, repo)
        resolved = task.status in SUCCESS and check_ok
    else:
        resolved = task.status == TaskStatus.ESCALATED
    diffstat = subprocess.run(["git", "diff", "--stat", f"{task.base_commit}..HEAD"], cwd=repo, text=True, capture_output=True).stdout
    b = task.budget
    return {
        "id": meta["id"],
        "category": meta["category"],
        "source": meta.get("source", "fixture"),
        "expect": meta["expect"],
        "status": task.status.value,
        "stop_reason": task.stop_reason,
        "resolved": resolved,
        "hidden_check": check_ok,
        "first_pass": resolved and task.iteration <= 1,
        "iterations": task.iteration,
        "lane": task.lane.value,
        "score": task.global_score,
        "tokens": b.tokens_used,
        "tokens_in": b.tokens_in,
        "tokens_out": b.tokens_out,
        "tokens_cached": b.tokens_cached,
        "model_calls": b.model_calls,
        "tool_calls": b.tool_calls,
        "runtime_s": elapsed,
        "diffstat": diffstat.strip().splitlines()[-1:] if diffstat.strip() else [],
        "workspace": str(repo),
        "check_output": "" if check_ok else check_out[-800:],
        "error": error,
        "fatal": fatal,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows) or 1
    solved = sum(r["resolved"] for r in rows)
    tokens = sum(r["tokens"] for r in rows)
    tokens_in = sum(r["tokens_in"] for r in rows)
    return {
        "tasks": len(rows),
        "resolved": solved,
        "success_rate": round(solved / n, 3),
        "first_pass_rate": round(sum(r["first_pass"] for r in rows) / n, 3),
        "avg_iterations": round(sum(r["iterations"] for r in rows) / n, 2),
        "avg_tool_calls": round(sum(r["tool_calls"] for r in rows) / n, 1),
        "total_tokens": tokens,
        "cached_tokens": sum(r["tokens_cached"] for r in rows),
        "cached_share": round(sum(r["tokens_cached"] for r in rows) / tokens_in, 3) if tokens_in else 0.0,
        "avg_tokens_per_task": round(tokens / n),
        "resolutions_per_1M_tokens": round(solved / tokens * 1_000_000, 2) if tokens else None,
        "total_runtime_s": round(sum(r["runtime_s"] for r in rows), 1),
    }


def run_bench(
    tasks_dir: Path,
    provider_name: str,
    only: list[str] | None = None,
    max_iterations: int = 5,
    results_dir: Path | None = None,
    on_result=None,
    provider: BaseProvider | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], Path | None]:
    workdir = Path(tempfile.mkdtemp(prefix="lcc-bench-"))
    rows = []
    out_path = None
    if results_dir:
        results_dir.mkdir(parents=True, exist_ok=True)
        out_path = results_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{provider_name}.jsonl"
    tasks = load_tasks(tasks_dir, only)
    if provider_name == "scripted":
        tasks = [t for t in tasks if (t["dir"] / "scripted.json").exists()]
    real = [t for t in tasks if "repo" in t]
    if real:
        py = bench_python(real)
        for t in real:
            t["_python"] = py
    for meta in tasks:
        if provider_name == "scripted" and not (meta["dir"] / "scripted.json").exists():
            continue  # no recorded model replies: running it would only measure an empty script
        row = run_one(meta, provider_name, max_iterations, workdir, provider)
        rows.append(row)
        if out_path:
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        if on_result:
            on_result(row)
        if row["fatal"]:
            break  # the same account error would fail every remaining task
    summary = summarize(rows)
    if rows and rows[-1]["fatal"]:
        summary["aborted"] = rows[-1]["error"].splitlines()[0]
    if out_path:
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"summary": summary, "provider": provider_name}) + "\n")
    return rows, summary, out_path
