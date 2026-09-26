from __future__ import annotations

import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from lcc.github_pr import create_pull_request
from lcc.model import get_provider
from lcc.orchestrator import Orchestrator, create_task
from lcc.schemas import TaskStatus
from lcc.state_machine import transition
from lcc.store import HarnessStore, write_handoff

app = typer.Typer(help="LCC autonomous software-engineering harness")
console = Console()


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip().removeprefix("export "), value.strip().strip('"').strip("'"))


@app.callback()
def _main() -> None:
    _load_dotenv()


def _store(path: Path | None = None) -> HarnessStore:
    return HarnessStore(path or Path.cwd())


@app.command()
def init(path: Path = typer.Argument(Path("."), help="Repository root")) -> None:
    store = HarnessStore(path)
    store.init_layout()
    console.print(f"Initialized harness at {store.harness}")


@app.command()
def ingest(
    objective: str = typer.Option(..., "--objective", "-o"),
    task_id: str = typer.Option("TASK-1", "--id"),
    issue_file: Path | None = typer.Option(None, "--issue-file"),
    repository: str = typer.Option("local/repo", "--repo"),
    workspace: Path = typer.Option(Path("."), "--workspace"),
) -> None:
    store = _store(workspace)
    body = issue_file.read_text(encoding="utf-8") if issue_file else objective
    task = create_task(store, task_id, objective, workspace, issue_body=body, repository=repository)
    console.print(f"Created {task.task_id} status={task.status.value}")


@app.command("run")
def run_task(
    workspace: Path = typer.Option(Path("."), "--workspace"),
) -> None:
    store = _store(workspace)
    task = store.load_task()
    if task is None:
        raise typer.BadParameter("No task_state.json. Run `lcc ingest` first.")
    orch = Orchestrator(store, get_provider())
    task = orch.run(task)
    console.print(f"Done status={task.status.value} score={task.global_score:.1f} stop={task.stop_reason}")


@app.command()
def status(workspace: Path = typer.Option(Path("."), "--workspace")) -> None:
    store = _store(workspace)
    task = store.load_task()
    if not task:
        console.print("No task")
        raise typer.Exit(1)
    table = Table(title=task.task_id)
    table.add_column("field")
    table.add_column("value")
    for k, v in {
        "status": task.status.value,
        "lane": task.lane.value,
        "iteration": str(task.iteration),
        "snapshot": task.context_snapshot or "",
        "branch": task.branch,
        "score": f"{task.global_score:.1f}",
        "tokens": f"{task.budget.tokens_used}/{task.budget.tokens}",
        "agent": task.current_agent or "",
        "stop": task.stop_reason or "",
    }.items():
        table.add_row(k, v)
    console.print(table)


@app.command()
def events(workspace: Path = typer.Option(Path("."), "--workspace"), limit: int = 30) -> None:
    store = _store(workspace)
    rows = store.events()[-limit:]
    for e in rows:
        console.print(f"{e.ts.isoformat()} {e.event} agent={e.agent} result={e.result}")


@app.command()
def handoff(workspace: Path = typer.Option(Path("."), "--workspace")) -> None:
    store = _store(workspace)
    task = store.load_task()
    if not task:
        raise typer.Exit(1)
    path = write_handoff(store, task, ["Continue from current status using state/task_state.json"])
    console.print(path.read_text(encoding="utf-8"))


@app.command()
def approve(workspace: Path = typer.Option(Path("."), "--workspace")) -> None:
    store = _store(workspace)
    task = store.load_task()
    if not task:
        raise typer.Exit(1)
    task.human_approval = True
    if task.status == TaskStatus.HUMAN_REVIEW:
        transition(task, TaskStatus.PR_READY)
    store.save_task(task)
    store.emit(task, "HUMAN_APPROVED")
    console.print("Approved. Task is PR_READY." if task.status == TaskStatus.PR_READY else "Recorded approval.")


@app.command()
def pr(workspace: Path = typer.Option(Path("."), "--workspace")) -> None:
    store = _store(workspace)
    task = store.load_task()
    if not task:
        raise typer.Exit(1)
    if not task.human_approval:
        raise typer.BadParameter("Human approval required before opening a PR.")
    url = create_pull_request(task)
    task.pr_url = url
    store.save_task(task)
    console.print(url)


@app.command()
def bench(
    provider: str = typer.Option(None, "--provider", "-p", help="mock | scripted | deepseek | gemini | openai | grok"),
    task: list[str] = typer.Option(None, "--task", "-t", help="Run only these task ids"),
    max_iterations: int = typer.Option(5, "--max-iterations"),
    tasks_dir: Path = typer.Option(Path("benchmarks/tasks"), "--tasks-dir"),
    results_dir: Path = typer.Option(Path("benchmarks/results"), "--results-dir"),
) -> None:
    """Run the benchmark suite and grade each task with its hidden check."""
    from lcc.bench import run_bench

    name = provider or os.environ.get("LCC_PROVIDER") or "mock"

    def show(r: dict) -> None:
        mark = "[green]PASS[/]" if r["resolved"] else "[red]FAIL[/]"
        console.print(
            f"{mark} {r['id']:<20} status={r['status']:<13} stop={r['stop_reason']} it={r['iterations']} "
            f"tokens={r['tokens']} tools={r['tool_calls']} {r['runtime_s']}s" + (f"\n  error: {r['error'].splitlines()[0]}" if r["error"] else "")
        )

    _, summary, out = run_bench(tasks_dir, name, task or None, max_iterations, results_dir, on_result=show)
    console.print_json(json.dumps(summary))
    if out:
        console.print(f"results: {out}")


@app.command("dump-state")
def dump_state(workspace: Path = typer.Option(Path("."), "--workspace")) -> None:
    store = _store(workspace)
    task = store.load_task()
    if not task:
        raise typer.Exit(1)
    console.print_json(json.loads(task.model_dump_json()))


if __name__ == "__main__":
    app()
