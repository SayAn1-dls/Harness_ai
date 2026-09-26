"""Benchmark runner: copy each fixture repo, run the harness, then grade with a hidden check the agent never saw."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

from lcc.model import BaseProvider, ScriptedProvider, get_provider
from lcc.orchestrator import Orchestrator, create_task
from lcc.schemas import TaskStatus
from lcc.store import HarnessStore

SUCCESS = {TaskStatus.HUMAN_REVIEW, TaskStatus.VERIFIED, TaskStatus.PR_READY}


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


def _provider_for(name: str, task_dir: Path) -> BaseProvider:
    if name == "scripted":
        path = task_dir / "scripted.json"
        return ScriptedProvider(json.loads(path.read_text(encoding="utf-8")) if path.exists() else {})
    return get_provider(name)


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


def run_one(meta: dict[str, Any], provider_name: str, max_iterations: int, workdir: Path) -> dict[str, Any]:
    task_dir: Path = meta["dir"]
    repo = workdir / meta["id"] / "repo"
    shutil.copytree(task_dir / "repo", repo)
    store = HarnessStore(repo)
    task = create_task(store, meta["id"], meta["objective"], repo, issue_body=meta["issue"],
                       budget_overrides={"max_iterations": max_iterations})
    t0 = time.monotonic()
    error = ""
    try:
        task = Orchestrator(store, _provider_for(provider_name, task_dir)).run(task)
    except Exception as exc:  # recorded, not fatal to the benchmark
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
        task = store.load_task() or task
    elapsed = round(time.monotonic() - t0, 1)

    check_ok, check_out = (False, "")
    if meta["expect"] == "resolve":
        check_ok, check_out = hidden_check(task_dir, repo)
        resolved = task.status in SUCCESS and check_ok
    else:
        resolved = task.status == TaskStatus.ESCALATED
    diffstat = subprocess.run(["git", "diff", "--stat", f"{task.base_commit}..HEAD"], cwd=repo, text=True, capture_output=True).stdout
    b = task.budget
    return {
        "id": meta["id"],
        "category": meta["category"],
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
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows) or 1
    solved = sum(r["resolved"] for r in rows)
    tokens = sum(r["tokens"] for r in rows)
    return {
        "tasks": len(rows),
        "resolved": solved,
        "success_rate": round(solved / n, 3),
        "first_pass_rate": round(sum(r["first_pass"] for r in rows) / n, 3),
        "avg_iterations": round(sum(r["iterations"] for r in rows) / n, 2),
        "avg_tool_calls": round(sum(r["tool_calls"] for r in rows) / n, 1),
        "total_tokens": tokens,
        "cached_tokens": sum(r["tokens_cached"] for r in rows),
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
) -> tuple[list[dict[str, Any]], dict[str, Any], Path | None]:
    workdir = Path(tempfile.mkdtemp(prefix="lcc-bench-"))
    rows = []
    out_path = None
    if results_dir:
        results_dir.mkdir(parents=True, exist_ok=True)
        out_path = results_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{provider_name}.jsonl"
    for meta in load_tasks(tasks_dir, only):
        row = run_one(meta, provider_name, max_iterations, workdir)
        rows.append(row)
        if out_path:
            with out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        if on_result:
            on_result(row)
    summary = summarize(rows)
    if out_path:
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"summary": summary, "provider": provider_name}) + "\n")
    return rows, summary, out_path
