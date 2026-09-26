from __future__ import annotations

import re
import time
from pathlib import Path

from lcc.agents import (
    CONTRACTS,
    intake_gate,
    needs_security,
    run_coder,
    run_context_agent,
    run_impact,
    run_intake,
    run_judge,
    run_planner,
    run_recovery,
    run_reviewer,
    run_security,
)
from lcc.constants import CONTEXT_SCORE_GATE, INTAKE_PASS_SCORE
from lcc.context_engine import persist_index, repo_map, retrieve, scan_repo, snapshot_meets_gate
from lcc.eval import global_task_score
from lcc.lanes import complexity_score, select_lane
from lcc.metering import MeteredProvider
from lcc.model import BaseProvider, get_provider
from lcc.rules_engine import discover_rules, rules_for_paths
from lcc.schemas import (
    FailureClass,
    IterationRecord,
    JudgeDecision,
    Lane,
    RecoveryAction,
    StopCondition,
    TaskState,
    TaskStatus,
    VerificationResult,
)
from lcc.state_machine import LEGAL_TRANSITIONS, transition
from lcc.store import HarnessStore, write_handoff
from lcc.tools import ToolPolicy, ToolRegistry, workspace_diff
from lcc.workspace import Workspace, assert_not_main

TINY_REPO_FILES = 8
TERMINAL = {TaskStatus.STOPPED, TaskStatus.ESCALATED, TaskStatus.HUMAN_REVIEW, TaskStatus.PR_READY}


class Orchestrator:
    """Owns the state machine, budgets, and agent activation. Agents never spawn agents."""

    def __init__(self, store: HarnessStore, provider: BaseProvider | None = None, *, coder_max_steps: int = 30) -> None:
        self.store = store
        self.raw_provider = provider or get_provider()
        self.llm: BaseProvider = self.raw_provider
        self.coder_max_steps = coder_max_steps
        self.index = None
        self.snapshot = None
        self.rules = []
        self.applicable = []
        self.plan = None
        self.findings = []
        self.last_verification: VerificationResult | None = None
        self.last_changed: list[str] = []
        self.scope_expansions: list[str] = []
        self.scores_history: list[float] = []
        self.intake_score = 0.0

    # ------------------------------------------------------------ main loop
    def run(self, task: TaskState, until_human: bool = True) -> TaskState:
        started = time.monotonic()
        self.llm = MeteredProvider(self.raw_provider, self.store, task, started)
        workspace = Workspace(Path(task.workspace))
        task.base_commit = workspace.ensure_git()
        workspace.create_task_branch(task)
        assert_not_main(task.branch)
        self.store.save_task(task)
        self.store.emit(task, "TASK_STARTED", provider=self.raw_provider.name, model=self.raw_provider.model)

        try:
            self._intake(task)
            if task.status != TaskStatus.ESCALATED:
                self._context(task)
            if task.status != TaskStatus.ESCALATED:
                self._rules(task)
                if task.lane == Lane.C:
                    self._impact(task)
                self._plan(task)
                self._iterate(task, until_human)
        except Exception as exc:
            self._fail_hard(task, exc)
            raise
        finally:
            self.llm.tick()  # type: ignore[attr-defined]
            self.store.save_task(task)
            write_handoff(self.store, task, self._next_steps(task))
        return task

    def _iterate(self, task: TaskState, until_human: bool) -> None:
        while True:
            stop = self._stop_reason(task)
            if stop:
                task.stop_reason = stop.value
                transition(task, TaskStatus.STOPPED)
                self.store.emit(task, "TASK_STOPPED", result="fail", reason=stop.value)
                return
            self._implement(task)
            ok = self._verify(task)
            self.findings = []
            if ok and task.lane != Lane.A:
                self._review(task)
            judge = self._judge(task, ok)
            score = global_task_score(
                task, self.intake_score, self.snapshot, self.plan, self.last_verification, judge,
                self.findings, self.last_changed,
            )
            task.global_score = score["total"]
            self.scores_history.append(task.global_score)
            self.store.save_sidecar("verification", {"score": score, "judge": judge.model_dump(mode="json")})
            if judge.decision == JudgeDecision.PASS and ok:
                transition(task, TaskStatus.VERIFIED)
                sha = Workspace(Path(task.workspace)).commit(f"lcc({task.task_id}): {task.objective[:60]}")
                task.stop_reason = StopCondition.VERIFIED_SUCCESS.value
                self.store.emit(task, "TASK_VERIFIED", result="success", commit=sha, score=task.global_score)
                if until_human:
                    transition(task, TaskStatus.HUMAN_REVIEW)
                self.store.save_task(task)
                return
            self._recover(task, ok)
            if task.status == TaskStatus.ESCALATED:
                return

    # ------------------------------------------------------------ phases
    def _intake(self, task: TaskState) -> None:
        transition(task, TaskStatus.ANALYZING)
        task.current_agent = "intake"
        self.store.save_task(task)
        result = run_intake(task, self.llm, self.store)
        gate = intake_gate(result)
        if gate == "iterate_intake":
            result = run_intake(
                task, self.llm, self.store,
                critique=f"score {result.score:.0f} < {INTAKE_PASS_SCORE}: fill in intent, requirements, "
                "acceptance criteria and constraints explicitly.",
            )
            gate = intake_gate(result)
        self.intake_score = result.score
        task.acceptance_criteria = result.acceptance_criteria
        task.constraints = result.constraints
        task.ambiguities = result.ambiguities + [f"BLOCKING: {a}" for a in result.blocking_ambiguities]
        task.risk = result.risk
        self.store.emit(task, "AGENT_COMPLETED", agent="intake", result="success", artifacts=["intake.json"], gate=gate, score=result.score)
        if gate == "escalate":
            task.stop_reason = "blocking_ambiguity" if result.blocking_ambiguities else "intake_high_risk"
            transition(task, TaskStatus.ESCALATED)
            self.store.emit(task, "TASK_ESCALATED", result="escalated", reason=task.stop_reason, questions=result.blocking_ambiguities)
        self.store.save_task(task)

    def _context(self, task: TaskState) -> None:
        transition(task, TaskStatus.CONTEXT_BUILDING)
        task.current_agent = "mapper"
        self.store.save_task(task)
        root = Path(task.workspace)
        self.index = scan_repo(root)
        persist_index(self.index, self.store.cache / "repo_index.json")
        self.store.write_text("repo_map.md", repo_map(self.index))
        self.rules = discover_rules(root)
        query = self._query(task)
        self.snapshot = retrieve(self.index, query, [r.id for r in self.rules])
        if not snapshot_meets_gate(self.snapshot):
            self.snapshot = retrieve(
                self.index, query, [r.id for r in self.rules], token_budget=20_000,
                extra_files=self.index.entry_points + self.index.tests[:10],
            )
        if not snapshot_meets_gate(self.snapshot) and len(self.index.files) > TINY_REPO_FILES:
            task.current_agent = "context"
            tools = self._tools(task, CONTRACTS["context"].permissions)
            extra = run_context_agent(task, self.llm, tools, self.snapshot)
            self.snapshot = retrieve(
                self.index, query, [r.id for r in self.rules], token_budget=20_000, extra_files=extra,
            )
            self.store.emit(task, "AGENT_COMPLETED", agent="context", result="success", added=extra, score=self.snapshot.score)
        task.context_snapshot = self.snapshot.snapshot_id
        self.store.write_json("context_snapshot.json", self.snapshot)
        self.store.save_sidecar("context", self.snapshot.model_dump(mode="json"))
        self.store.emit(task, "AGENT_COMPLETED", agent="mapper", result="success", artifacts=["context_snapshot.json"], score=self.snapshot.score)

        if not snapshot_meets_gate(self.snapshot) and len(self.index.files) > TINY_REPO_FILES:
            task.stop_reason = "missing_context"
            self.store.emit(task, "CONTEXT_GATE_FAILED", result="fail", score=self.snapshot.score, gate=CONTEXT_SCORE_GATE)
            transition(task, TaskStatus.ESCALATED)
            self.store.save_task(task)
            return

        score = complexity_score(task, len(self.snapshot.files[:8]), len(self.index.languages), len(self.rules), dep_depth=2)
        task.lane = select_lane(score)
        self.store.emit(task, "DECISION", decision=f"lane={task.lane.value}", complexity=score)
        self.store.save_task(task)

    def _rules(self, task: TaskState) -> None:
        transition(task, TaskStatus.RULE_RESOLUTION)
        task.current_agent = "rules"
        self.applicable = rules_for_paths(self.rules, self.snapshot.files if self.snapshot else [])
        task.applicable_rules = [r.id for r in self.applicable]
        self.store.write_json("rules.json", [r.model_dump() for r in self.applicable])
        self.store.emit(task, "AGENT_COMPLETED", agent="rules", result="success", artifacts=["rules.json"])

    def _impact(self, task: TaskState) -> None:
        transition(task, TaskStatus.IMPACT_ANALYSIS)
        task.current_agent = "impact"
        files = self.snapshot.files if self.snapshot else []
        report = run_impact(self.index, task, files[:12])
        task.affected_files = report["affected_files"]
        task.potentially_affected_files = report["potentially_affected_files"]
        self.store.write_json("impact.json", report)
        self.store.emit(task, "AGENT_COMPLETED", agent="impact", result="success", artifacts=["impact.json"])

    def _plan(self, task: TaskState) -> None:
        transition(task, TaskStatus.PLANNING)
        task.current_agent = "planner"
        if not task.affected_files and self.snapshot:
            task.affected_files = self.snapshot.files[:8]
        self.plan = run_planner(task, self.llm, self._summary(8, 1500), self.store)
        task.plan_version = self.plan.version
        transition(task, TaskStatus.PLAN_VALIDATION)
        transition(task, TaskStatus.READY_TO_EXECUTE)
        self.store.emit(task, "AGENT_COMPLETED", agent="planner", result="success", artifacts=["plan.json"])
        self.store.save_task(task)

    def _implement(self, task: TaskState) -> None:
        transition(task, TaskStatus.IMPLEMENTING)
        task.iteration += 1
        task.current_agent = "coder"
        self.store.save_task(task)
        tools = self._tools(
            task,
            CONTRACTS["coder"].permissions,
            allowed=self.plan.allowed_files if self.plan else [],
            forbidden=self.plan.forbidden_files if self.plan else [],
        )
        result = run_coder(task, self.llm, tools, self.plan, self.store, self.snapshot, self.applicable, self.coder_max_steps)
        self.last_changed = result.data["changed"]
        self.scope_expansions = sorted(set(self.scope_expansions) | set(result.data["scope_expansions"]))
        task.affected_files = list(dict.fromkeys(task.affected_files + self.last_changed))
        self.store.emit(
            task, "AGENT_COMPLETED", agent="coder", result="success" if result.success else "fail",
            artifacts=[f"coder_{task.iteration}.json"], changed=self.last_changed,
            scope_expansions=result.data["scope_expansions"], stop=result.data["stop_reason"],
            steps=result.data["steps"],
        )

    def _verify(self, task: TaskState) -> bool:
        transition(task, TaskStatus.TESTING)
        task.current_agent = "verifier"
        tools = self._tools(task, CONTRACTS["verifier"].permissions)
        test_res = tools.run_test()
        lint_res = tools.run_lint()
        lint_ok = bool(lint_res.get("ok") or lint_res.get("skipped"))
        ok = bool(test_res.get("ok")) and lint_ok
        evidence = [
            f"tests: rc={test_res.get('returncode')} run={test_res.get('tests_run')} failed={test_res.get('tests_failed')}",
            f"lint: ok={lint_res.get('ok')} skipped={bool(lint_res.get('skipped'))}",
        ]
        output = f"{test_res.get('stdout') or ''}\n{test_res.get('stderr') or ''}".strip()
        if not test_res.get("tests_run"):
            output += "\n[harness] tests_run=0: no tests were collected, which is not a pass."
        if not lint_ok:
            output += f"\n[lint]\n{lint_res.get('stdout') or ''}{lint_res.get('stderr') or ''}"
        evidence.append(output[-1500:])
        self.last_verification = VerificationResult(
            passed=ok, commands=[test_res, lint_res], acceptance=task.acceptance_criteria, evidence=evidence,
            score=100 if ok else 0,
        )
        task.verification = {
            "passed": ok,
            "tests_run": test_res.get("tests_run"),
            "tests_failed": test_res.get("tests_failed"),
            "lint_ok": lint_ok,
        }
        self.store.write_json(f"verification_{task.iteration}.json", self.last_verification)
        self.store.emit(task, "AGENT_COMPLETED", agent="verifier", result="success" if ok else "fail",
                        artifacts=[f"verification_{task.iteration}.json"], **task.verification)
        task.last_failure = None if ok else output[-6000:]
        return ok

    def _review(self, task: TaskState) -> None:
        transition(task, TaskStatus.REVIEWING)
        task.current_agent = "reviewer"
        diff = workspace_diff(Path(task.workspace))
        evidence = "\n".join(self.last_verification.evidence) if self.last_verification else ""
        self.findings = run_reviewer(task, self.llm, diff, self.store, evidence, self.scope_expansions)
        if needs_security(task):
            task.current_agent = "security"
            self.findings.extend(run_security(task, diff, self.store))
        self.store.emit(task, "AGENT_COMPLETED", agent="reviewer", result="success", findings=len(self.findings))

    def _judge(self, task: TaskState, ok: bool):
        transition(task, TaskStatus.JUDGING)
        task.current_agent = "judge"
        judge = run_judge(task, ok, self.findings, self.store)
        self.store.emit(task, "AGENT_COMPLETED", agent="judge", result=judge.decision.value, blocking=len(judge.blocking_findings))
        return judge

    def _recover(self, task: TaskState, ok: bool) -> None:
        transition(task, TaskStatus.FAILED)
        transition(task, TaskStatus.DIAGNOSING)
        task.current_agent = "recovery"
        root = Path(task.workspace)
        diff = workspace_diff(root)
        failure = self.last_verification.commands[0] if self.last_verification else {"stderr": task.last_failure or ""}
        if ok:  # tests passed but the judge found blocking issues
            failure = {"stdout": "", "stderr": "Review blocking findings:\n" + "\n".join(
                f"- {f.description}: {f.evidence[:2]}" for f in self.findings)}
            task.last_failure = failure["stderr"]
        rec = run_recovery(task, failure, diff, self.llm, self.store, self.findings)

        sig = _failure_signature((task.last_failure or "")[-1500:])
        prev_sig = _failure_signature(task.history[-1].new_observations[0]) if task.history and task.history[-1].new_observations else None
        task.repeated_failure_count = task.repeated_failure_count + 1 if sig and sig == prev_sig else 1

        record = IterationRecord(
            iteration=task.iteration,
            previous_state=TaskStatus.JUDGING.value,
            new_observations=[(task.last_failure or "")[-1500:]],
            failed_assumptions=[rec["failed_assumption"]] if rec["failed_assumption"] else [],
            changed_files=list(self.last_changed),
            remaining_requirements=[c.text for c in task.acceptance_criteria if not c.satisfied],
            verification_delta=[str(task.verification)],
            failure_class=FailureClass(rec["class"]),
            recovery_action=RecoveryAction(rec["action"]),
        )
        self.store.emit(task, "RECOVERY", result=rec["action"], klass=rec["class"], repeated=task.repeated_failure_count)

        if rec["action"] == RecoveryAction.ESCALATE.value:
            task.history.append(record)
            task.stop_reason = StopCondition.UNSAFE_ACTION_REQUIRED.value
            transition(task, TaskStatus.ESCALATED)
            self.store.save_task(task)
            return
        if rec["action"] == RecoveryAction.ROLLBACK.value:
            Workspace(root).reset_hard(task.base_commit)
            self.store.emit(task, "DECISION", decision="rollback to base commit", commit=task.base_commit)

        transition(task, TaskStatus.CONTEXT_UPDATE)
        new_files: list[str] = []
        if self.index:
            before = set(self.snapshot.files) if self.snapshot else set()
            extra = rec["files_to_inspect"] + _paths_in(task.last_failure or "", self.index.files) + task.affected_files
            self.snapshot = retrieve(
                self.index, f"{self._query(task)}\n{(task.last_failure or '')[-2000:]}", task.applicable_rules,
                token_budget=16_000, extra_files=extra,
            )
            task.context_snapshot = self.snapshot.snapshot_id
            new_files = [f for f in self.snapshot.files if f not in before]
        record.new_context = new_files

        transition(task, TaskStatus.REPLANNING)
        task.current_agent = "planner"
        note = f"class={rec['class']} action={rec['action']}\nfailed assumption: {rec['failed_assumption']}\nguidance: {rec['guidance']}"
        task.history.append(record)
        self.plan = run_planner(task, self.llm, self._summary(6, 1200), self.store, recovery_note=note)
        task.plan_version = self.plan.version
        record.new_plan = [s.action for s in self.plan.steps]
        self.store.write_json(f"iteration_{task.iteration}.json", record)
        self.store.save_task(task)

    # ------------------------------------------------------------ helpers
    def _tools(self, task: TaskState, permissions: dict[str, bool], allowed=None, forbidden=None) -> ToolRegistry:
        policy = ToolPolicy(permissions, allowed_paths=allowed, forbidden_paths=forbidden)
        return ToolRegistry(Path(task.workspace), policy, task.budget, index=self.index)

    def _query(self, task: TaskState) -> str:
        return f"{task.objective}\n{task.issue_body}\n" + "\n".join(c.text for c in task.acceptance_criteria)

    def _summary(self, n: int, chars: int) -> str:
        if not self.snapshot:
            return ""
        return "\n\n".join(f"# {p.path}\n{p.excerpt[:chars]}" for p in self.snapshot.packets[:n])

    def _stop_reason(self, task: TaskState) -> StopCondition | None:
        self.llm.tick()  # type: ignore[attr-defined]
        if task.iteration >= task.budget.max_iterations:
            return StopCondition.MAX_ITERATIONS
        if task.budget.exhausted():
            return StopCondition.BUDGET_EXCEEDED
        if task.repeated_failure_count >= 2:
            return StopCondition.SAME_FAILURE_REPEATED
        h = self.scores_history
        if len(h) >= 3 and h[-1] < h[-2] - 5 and h[-2] < h[-3] - 5:
            return StopCondition.CONFIDENCE_DECREASING
        return None

    def _fail_hard(self, task: TaskState, exc: Exception) -> None:
        task.last_failure = f"{type(exc).__name__}: {exc}"
        task.stop_reason = task.stop_reason or "harness_error"
        self.store.emit(task, "TASK_FAILED", result="error", error=task.last_failure)
        if task.status in TERMINAL:
            return
        if TaskStatus.FAILED in LEGAL_TRANSITIONS.get(task.status, set()):
            transition(task, TaskStatus.FAILED)
        transition(task, TaskStatus.STOPPED)

    def _next_steps(self, task: TaskState) -> list[str]:
        if task.status == TaskStatus.HUMAN_REVIEW:
            return ["Review the diff on the task branch", "Run `lcc approve` then `lcc pr` if acceptable"]
        if task.status == TaskStatus.ESCALATED:
            qs = [a for a in task.ambiguities if a.startswith("BLOCKING")]
            return (qs or [f"Resolve: {task.stop_reason}"]) + ["Update the issue text and re-ingest"]
        if task.status == TaskStatus.STOPPED:
            return [f"Stopped: {task.stop_reason}", "Inspect harness/artifacts/iteration_*.json and the last verification",
                    "Adjust budget or issue, then re-ingest"]
        return ["Continue with lcc run"]


def _failure_signature(text: str) -> str:
    """Normalize test output so timings and addresses don't make identical failures look different."""
    keep = [line for line in text.splitlines() if re.search(r"(FAIL|ERROR|Error|assert|failed)", line)]
    sig = "\n".join(keep[-12:])
    sig = re.sub(r"\d+\.\d+s", "", sig)
    return re.sub(r"0x[0-9a-f]+", "", sig)


def _paths_in(text: str, known: list[str]) -> list[str]:
    return [f for f in known if f in text][:6]


def create_task(
    store: HarnessStore,
    task_id: str,
    objective: str,
    workspace: Path,
    issue_body: str = "",
    repository: str = "local/repo",
    budget_overrides: dict | None = None,
) -> TaskState:
    store.init_layout()
    task = TaskState(
        task_id=task_id,
        repository=repository,
        workspace=str(Path(workspace).resolve()),
        objective=objective,
        issue_body=issue_body,
        branch=f"agent/{task_id}",
    )
    for k, v in (budget_overrides or {}).items():
        setattr(task.budget, k, v)
    store.save_task(task)
    store.emit(task, "TASK_RECEIVED")
    write_handoff(store, task, ["Run intake and context via lcc run"])
    return task
