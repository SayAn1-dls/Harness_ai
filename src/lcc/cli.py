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
    """Minimal .env loader (KEY=VALUE lines). Non-empty environment variables win; `make` passes an unset
    AI_API_KEY through as an empty string, which must not hide the value in .env."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if not os.environ.get(key):
            os.environ[key] = value.strip().strip('"').strip("'")


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
    provider: str = typer.Option(None, "--provider", "-p", help="auto | scripted | mock | deepseek | gemini | openai | anthropic | openrouter | groq | grok | custom"),
    task: list[str] = typer.Option(None, "--task", "-t", help="Run only these task ids"),
    max_iterations: int = typer.Option(5, "--max-iterations"),
    tasks_dir: Path = typer.Option(Path("benchmarks/tasks"), "--tasks-dir"),
    results_dir: Path = typer.Option(Path("benchmarks/results"), "--results-dir"),
    min_resolved: int = typer.Option(0, "--min-resolved", help="Exit 1 if fewer tasks are resolved (for CI / make test)"),
    validate: bool = typer.Option(False, "--validate", help="Only check the real tasks: hidden tests fail at base, pass at fix"),
) -> None:
    """Run the benchmark suite and grade each task with its hidden check."""
    from lcc.bench import run_bench, validate_real

    if validate:
        rows = validate_real(tasks_dir, task or None)
        for r in rows:
            mark = "[green]valid[/]" if r["valid"] else "[red]INVALID[/]"
            console.print(f"{mark} {r['id']:<32} fails_on_base={r['fails_on_base']} passes_on_fix={r['passes_on_fix']} {r['detail']}")
        bad = sum(not r["valid"] for r in rows)
        console.print(f"{len(rows) - bad}/{len(rows)} real tasks valid")
        raise typer.Exit(1 if bad else 0)

    from lcc.config import load_config

    from lcc.model import ProviderError

    name = provider or load_config().model.provider
    shared = None
    if name not in {"scripted", "mock", "oracle"}:
        try:  # resolve and check the model once: a bad key must not burn through every task
            shared = get_provider(name)
            shared.preflight()
        except ProviderError as exc:
            console.print(f"[red]Cannot start the benchmark:[/] {exc}")
            raise typer.Exit(2) from None
        console.print(f"model: {shared.name} / {shared.model}")

    def show(r: dict) -> None:
        mark = "[green]PASS[/]" if r["resolved"] else "[red]FAIL[/]"
        console.print(
            f"{mark} {r['id']:<20} status={r['status']:<13} stop={r['stop_reason']} it={r['iterations']} "
            f"tokens={r['tokens']} tools={r['tool_calls']} {r['runtime_s']}s" + (f"\n  error: {r['error'].splitlines()[0]}" if r["error"] else "")
        )

    _, summary, out = run_bench(tasks_dir, name, task or None, max_iterations, results_dir, on_result=show,
                                provider=shared)
    console.print_json(json.dumps(summary))
    if out:
        console.print(f"results: {out}")
    if summary.get("aborted"):
        console.print(f"[red]aborted:[/] {summary['aborted']}")
        raise typer.Exit(2)
    if summary["resolved"] < min_resolved:
        console.print(f"[red]resolved {summary['resolved']} < required {min_resolved}[/]")
        raise typer.Exit(1)


@app.command()
def start(
    issue: str = typer.Option(None, "--issue", "-i", help="GitHub issue URL, path to an issue file, or issue text"),
    repo: str = typer.Option(None, "--repo", "-r", help="Target repository: local path, git URL, or owner/name"),
    provider: str = typer.Option(None, "--provider", "-p", help="Override [model].provider from lcc.config.toml"),
    once: bool = typer.Option(False, "--once", help="Exit after one issue"),
    base: str = typer.Option(None, "--base", "-b", help="Base commit/ref to check out before fixing"),
    auto: bool = typer.Option(False, "--auto", help="No issue: find bugs/optimizations in --repo, fix them, open PRs"),
    pr: bool = typer.Option(None, "--pr/--no-pr", help="Open pull requests for verified fixes in auto mode"),
) -> None:
    """Evaluation mode (what `make run` launches): read an issue, fix it in the repo, report the verified patch.
    Given only a repository link, it finds the problems itself, fixes them and opens pull requests."""
    from lcc.session import start as run_session

    raise typer.Exit(run_session(issue or None, repo or None, provider, once, base or None, auto, pr))


@app.command()
def doctor() -> None:
    """Check the runtime: python, git, config, AI_API_KEY presence, provider resolution, and one tiny model call."""
    import shutil
    import sys

    from lcc.config import api_key, load_config
    from lcc.model import ProviderError

    cfg = load_config()
    rows = [
        ("python", sys.version.split()[0], sys.version_info >= (3, 11)),
        ("git", shutil.which("git") or "missing", bool(shutil.which("git"))),
        ("config", str(cfg.path or "built-in defaults"), True),
        ("AI_API_KEY", "set" if api_key() else "missing", bool(api_key())),
    ]
    try:
        p = get_provider()
        rows.append(("provider", f"{p.name} ({getattr(p, 'base_url', '')})", True))
        try:
            model = p.preflight()
            rows.append(("model", f"{model} answered (temperature={cfg.model.temperature})", True))
        except ProviderError as exc:
            rows.append(("model", str(exc)[:400], False))
    except ProviderError as exc:
        rows.append(("provider", str(exc), False))
    ok = True
    for name, value, good in rows:
        ok &= good
        console.print(f"{'[green]ok [/]' if good else '[red]!! [/]'} {name:<11} {value}")
    raise typer.Exit(0 if ok else 1)


@app.command()
def verify(
    repo: Path = typer.Option(Path("."), "--repo", help="Git repository that contains both commits"),
    proof: Path = typer.Option(None, "--proof", help="A proof file written by the harness (outputs/<task>.proof.json)"),
    base: str = typer.Option(None, "--base", help="Commit before the fix"),
    head: str = typer.Option(None, "--head", help="Commit or branch with the fix"),
    tests: str = typer.Option(None, "--tests", help="Comma-separated test files or ids (default: tests the fix changed)"),
) -> None:
    """Replay a fix's proof: its tests must FAIL on the original code and PASS with the fix. Changes nothing."""
    from lcc.proof import verify as run_verify

    targets = [t for t in (tests or "").split(",") if t.strip()] or None
    if proof:
        data = json.loads(proof.read_text(encoding="utf-8"))
        base, head = base or data["base"], head or data["head"]
        targets = targets or data.get("targets") or None
    if not base or not head:
        raise typer.BadParameter("give --proof, or --base and --head")
    res = run_verify(repo, base, head, targets)
    if "reason" in res:
        console.print(f"[red]cannot verify:[/] {res['reason']}")
        raise typer.Exit(1)
    console.print(f"targets: {', '.join(res['targets'])}")
    console.print(("[green]✓[/]" if res["fails_on_base"] else "[red]✗[/]") + f" on the original code ({base[:10]}) the tests "
                  + ("FAIL: the bug is real" if res["fails_on_base"] else "already pass: this proves nothing"))
    console.print(("[green]✓[/]" if res["passes_on_head"] else "[red]✗[/]") + f" with the fix ({head[:10]}) the tests "
                  + ("PASS" if res["passes_on_head"] else "still FAIL"))
    console.print("[bold green]PROOF HOLDS[/]" if res["valid"] else "[bold red]PROOF DOES NOT HOLD[/]")
    if not res["valid"]:
        console.print(res["head_output"][-800:] if res["fails_on_base"] else res["base_output"][-800:])
    raise typer.Exit(0 if res["valid"] else 1)


@app.command("dump-state")
def dump_state(workspace: Path = typer.Option(Path("."), "--workspace")) -> None:
    store = _store(workspace)
    task = store.load_task()
    if not task:
        raise typer.Exit(1)
    console.print_json(json.loads(task.model_dump_json()))


if __name__ == "__main__":
    app()
