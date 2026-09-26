from __future__ import annotations

from pathlib import Path

from lcc.agents import (
    intake_gate,
    needs_security,
    run_coder,
    run_impact,
    run_intake,
    run_judge,
    run_planner,
    run_recovery,
    run_reviewer,
    run_security,
)
from lcc.constants import CONTEXT_SCORE_GATE
from lcc.context_engine import persist_index, repo_map, retrieve, scan_repo, snapshot_meets_gate
from lcc.eval import global_task_score
from lcc.lanes import agents_for_lane, complexity_score, select_lane
from lcc.model import ModelProvider, get_provider
from lcc.rules_engine import discover_rules, rules_for_paths
from lcc.schemas import (
    IterationRecord,
    Lane,
    StopCondition,
    TaskState,
    TaskStatus,
    VerificationResult,
)
from lcc.state_machine import transition
from lcc.store import HarnessStore, write_handoff
from lcc.tools import ToolPolicy, ToolRegistry
from lcc.workspace import Workspace, assert_not_main


class Orchestrator:
    def __init__(self, store: HarnessStore, provider: ModelProvider | None = None) -> None:
        self.store = store
        self.provider = provider or get_provider()
        self.index = None
        self.snapshot = None
        self.rules = []
        self.plan = None
        self.findings = []
        self.last_verification: VerificationResult | None = None
        self.scores_history: list[float] = []

    def run(self, task: TaskState, until_human: bool = True) -> TaskState:
        workspace = Workspace(Path(task.workspace))
        task.base_commit = workspace.ensure_git()
        workspace.create_task_branch(task)
        assert_not_main(task.branch)
        self.store.save_task(task)
        self.store.emit(task, "TASK_STARTED")

        try:
            self._intake(task)
            self._context(task)
            self._rules(task)
            if task.status == TaskStatus.ESCALATED:
                write_handoff(self.store, task, self._next_steps(task))
                return task
            if task.lane == Lane.C or "impact" in agents_for_lane(task.lane, needs_security(task)):
                self._impact(task)
            self._plan(task)
            while True:
                stop = self._stop_reason(task)
                if stop:
                    task.stop_reason = stop.value
                    if stop == StopCondition.VERIFIED_SUCCESS:
                        break
                    transition(task, TaskStatus.STOPPED)
                    self.store.save_task(task)
                    break
                self._implement(task)
                ok = self._verify(task)
                if task.lane != Lane.A:
                    self._review(task)
                judge = self._judge(task, ok)
                score = global_task_score(task, self.snapshot.score if self.snapshot else 0, self.last_verification, judge, self.findings)
                task.global_score = score["total"]
                self.scores_history.append(task.global_score)
                self.store.save_sidecar("verification", {"score": score, "judge": judge.model_dump(mode="json")})
                if judge.decision.value == "PASS" and ok:
                    transition(task, TaskStatus.VERIFIED)
                    self.store.emit(task, "TASK_VERIFIED", result="success")
                    if until_human:
                        transition(task, TaskStatus.HUMAN_REVIEW)
                    self.store.save_task(task)
                    break
                self._recover(task, ok)
            write_handoff(self.store, task, self._next_steps(task))
            return task
        except Exception as exc:
            task.last_failure = str(exc)
            self.store.emit(task, "TASK_FAILED", result="error", error=str(exc))
            try:
                if task.status not in {TaskStatus.STOPPED, TaskStatus.ESCALATED}:
                    if task.status != TaskStatus.FAILED:
                        # jump via FAILED if legal else force status
                        try:
                            transition(task, TaskStatus.FAILED)
                        except Exception:
                            task.status = TaskStatus.FAILED
            except Exception:
                task.status = TaskStatus.FAILED
            self.store.save_task(task)
            write_handoff(self.store, task, ["Inspect harness/state/events.jsonl", "Fix the failure", "Re-run lcc run"])
            raise

    def _intake(self, task: TaskState) -> None:
        transition(task, TaskStatus.ANALYZING)
        task.current_agent = "intake"
        self.store.save_task(task)
        result = run_intake(task, self.provider, self.store)
        task.acceptance_criteria = result.acceptance_criteria
        task.constraints = result.constraints
        task.ambiguities = result.ambiguities
        task.risk = result.risk
        task.objective = result.problem or task.objective
        gate = intake_gate(result)
        self.store.emit(task, "AGENT_COMPLETED", agent="intake", result="success", artifacts=["intake.json"], gate=gate)
        if gate == "escalate":
            transition(task, TaskStatus.ESCALATED)
            task.stop_reason = "intake_high_risk"
            self.store.save_task(task)

    def _context(self, task: TaskState) -> None:
        if task.status == TaskStatus.ESCALATED:
            return
        transition(task, TaskStatus.CONTEXT_BUILDING)
        task.current_agent = "mapper"
        self.store.save_task(task)
        root = Path(task.workspace)
        self.index = scan_repo(root)
        persist_index(self.index, self.store.cache / "repo_index.json")
        rmap = repo_map(self.index)
        self.store.write_text("repo_map.md", rmap)
        self.rules = discover_rules(root)
        self.snapshot = retrieve(
            self.index,
            f"{task.objective}\n{task.issue_body}",
            [r.id for r in self.rules],
        )
        # If score is below gate, expand budget once.
        if not snapshot_meets_gate(self.snapshot):
            self.snapshot = retrieve(
                self.index,
                f"{task.objective}\n{task.issue_body}\n{' '.join(self.index.tests[:20])}",
                [r.id for r in self.rules],
                token_budget=20_000,
                extra_files=self.index.entry_points + self.index.tests[:10],
            )
        task.context_snapshot = self.snapshot.snapshot_id
        self.store.write_json("context_snapshot.json", self.snapshot)
        self.store.save_sidecar("context", self.snapshot.model_dump(mode="json"))
        self.store.emit(task, "AGENT_COMPLETED", agent="mapper", result="success", artifacts=["context_snapshot.json"])
        if not snapshot_meets_gate(self.snapshot) and self.snapshot.score < CONTEXT_SCORE_GATE:
            # still allow trivial tasks on tiny repos
            if len(self.index.files) > 8:
                self.store.emit(task, "CONTEXT_GATE_FAILED", result="fail", score=self.snapshot.score)

        langs = len(self.index.languages)
        score = complexity_score(task, len(self.snapshot.files), langs, len(self.rules), dep_depth=2)
        task.lane = select_lane(score)
        self.store.emit(task, "DECISION", decision=f"lane={task.lane.value}", complexity=score)

    def _rules(self, task: TaskState) -> None:
        if task.status in {TaskStatus.ESCALATED, TaskStatus.STOPPED}:
            return
        transition(task, TaskStatus.RULE_RESOLUTION)
        task.current_agent = "rules"
        applicable = rules_for_paths(self.rules, self.snapshot.files if self.snapshot else [])
        task.applicable_rules = [r.id for r in applicable]
        self.store.write_json("rules.json", [r.model_dump() for r in applicable])
        self.store.emit(task, "AGENT_COMPLETED", agent="rules", result="success", artifacts=["rules.json"])

    def _impact(self, task: TaskState) -> None:
        if task.status in {TaskStatus.ESCALATED, TaskStatus.STOPPED}:
            return
        transition(task, TaskStatus.IMPACT_ANALYSIS)
        task.current_agent = "impact"
        files = self.snapshot.files if self.snapshot else []
        report = run_impact(self.index, task, files[:12])
        task.affected_files = report["affected_files"]
        task.potentially_affected_files = report["potentially_affected_files"]
        self.store.write_json("impact.json", report)
        self.store.emit(task, "AGENT_COMPLETED", agent="impact", result="success", artifacts=["impact.json"])

    def _plan(self, task: TaskState) -> None:
        if task.status in {TaskStatus.ESCALATED, TaskStatus.STOPPED}:
            return
        if task.status == TaskStatus.RULE_RESOLUTION:
            transition(task, TaskStatus.PLANNING)
        elif task.status == TaskStatus.IMPACT_ANALYSIS:
            transition(task, TaskStatus.PLANNING)
        task.current_agent = "planner"
        summary = ""
        if self.snapshot:
            summary = "\n\n".join(f"# {p.path}\n{p.excerpt[:1500]}" for p in self.snapshot.packets[:8])
        if not task.affected_files and self.snapshot:
            task.affected_files = self.snapshot.files[:8]
        self.plan = run_planner(task, self.provider, summary, self.store)
        task.plan_version = self.plan.version
        transition(task, TaskStatus.PLAN_VALIDATION)
        transition(task, TaskStatus.READY_TO_EXECUTE)
        self.store.emit(task, "AGENT_COMPLETED", agent="planner", result="success", artifacts=["plan.json"])
        self.store.save_task(task)

    def _implement(self, task: TaskState) -> None:
        if task.status == TaskStatus.READY_TO_EXECUTE:
            transition(task, TaskStatus.IMPLEMENTING)
        elif task.status == TaskStatus.REPLANNING:
            transition(task, TaskStatus.IMPLEMENTING)
        task.iteration += 1
        task.current_agent = "coder"
        self.store.save_task(task)
        tools = ToolRegistry(
            Path(task.workspace),
            ToolPolicy({"read_repository": True, "write_repository": True, "shell": False}),
            task.budget,
        )
        summary = ""
        if self.snapshot:
            summary = "\n\n".join(f"# {p.path}\n{p.excerpt[:1200]}" for p in self.snapshot.packets[:8])
        result = run_coder(task, self.provider, tools, self.plan, self.store, summary)
        if result.data.get("changed"):
            task.affected_files = list(dict.fromkeys(task.affected_files + result.data["changed"]))
        self.store.emit(task, "AGENT_COMPLETED", agent="coder", result="success" if result.success else "fail", artifacts=["coder.json"])

    def _verify(self, task: TaskState) -> bool:
        transition(task, TaskStatus.TESTING)
        task.current_agent = "verifier"
        tools = ToolRegistry(
            Path(task.workspace),
            ToolPolicy({"read_repository": True, "write_repository": False, "shell": True}),
            task.budget,
        )
        test_res = tools.run_test()
        lint_res = tools.run_lint()
        ok = bool(test_res.get("ok"))
        # If pytest is missing, treat skipped collection as environment but still record
        evidence = [f"test ok={test_res.get('ok')} rc={test_res.get('returncode')}"]
        if test_res.get("stdout"):
            evidence.append(str(test_res["stdout"])[-1500:])
        if test_res.get("stderr"):
            evidence.append(str(test_res["stderr"])[-1500:])
        self.last_verification = VerificationResult(
            passed=ok,
            commands=[test_res, lint_res],
            acceptance=task.acceptance_criteria,
            evidence=evidence,
            score=80 if ok else 20,
        )
        task.verification = self.last_verification.model_dump(mode="json")
        self.store.write_json("verification.json", self.last_verification)
        self.store.emit(task, "AGENT_COMPLETED", agent="verifier", result="success" if ok else "fail", artifacts=["verification.json"])
        if not ok:
            task.last_failure = (test_res.get("stderr") or test_res.get("stdout") or "tests failed")[:500]
        return ok

    def _review(self, task: TaskState) -> None:
        if task.status == TaskStatus.TESTING:
            transition(task, TaskStatus.REVIEWING)
        task.current_agent = "reviewer"
        tools = ToolRegistry(
            Path(task.workspace),
            ToolPolicy({"read_repository": True}),
            task.budget,
        )
        diff = str(tools.git_diff().get("diff") or "")
        self.findings = run_reviewer(task, self.provider, diff, self.store)
        if needs_security(task):
            self.findings.extend(run_security(task, diff, self.store))
        self.store.emit(task, "AGENT_COMPLETED", agent="reviewer", result="success", artifacts=["review.json"])

    def _judge(self, task: TaskState, ok: bool):
        if task.status == TaskStatus.REVIEWING:
            transition(task, TaskStatus.JUDGING)
        elif task.status == TaskStatus.TESTING:
            transition(task, TaskStatus.JUDGING)
        task.current_agent = "judge"
        judge = run_judge(task, ok, self.findings, self.store)
        self.store.emit(task, "AGENT_COMPLETED", agent="judge", result=judge.decision.value, artifacts=["judge.json"])
        return judge

    def _recover(self, task: TaskState, ok: bool) -> None:
        if task.status == TaskStatus.JUDGING:
            transition(task, TaskStatus.FAILED)
        task.current_agent = "recovery"
        transition(task, TaskStatus.DIAGNOSING)
        failure = {"stdout": "", "stderr": task.last_failure or "verification failed", "ok": ok}
        if self.last_verification and self.last_verification.commands:
            failure = self.last_verification.commands[0]
        rec = run_recovery(task, failure, self.provider, self.store)
        record = IterationRecord(
            iteration=task.iteration,
            previous_state=task.status.value,
            new_observations=[str(rec)],
            failed_assumptions=[task.last_failure or ""],
            remaining_requirements=[c.text for c in task.acceptance_criteria if not c.satisfied],
            failure_class=rec["class"],  # type: ignore[arg-type]
            recovery_action=rec["action"],  # type: ignore[arg-type]
        )
        self.store.write_json(f"iteration_{task.iteration}.json", record)
        if rec["action"] == "escalate":
            transition(task, TaskStatus.ESCALATED)
            task.stop_reason = StopCondition.UNSAFE_ACTION_REQUIRED.value
            self.store.save_task(task)
            return
        if rec["action"] == "rollback":
            Workspace(Path(task.workspace)).reset_hard(task.base_commit)
        # same failure detection
        sig = (task.last_failure or "")[:200]
        if getattr(self, "_last_sig", None) == sig and sig:
            task.repeated_failure_count += 1
        else:
            task.repeated_failure_count = 1
            self._last_sig = sig
        transition(task, TaskStatus.CONTEXT_UPDATE)
        if self.index:
            self.snapshot = retrieve(
                self.index,
                f"{task.objective}\n{task.last_failure}",
                task.applicable_rules,
                extra_files=task.affected_files,
            )
            task.context_snapshot = self.snapshot.snapshot_id
        transition(task, TaskStatus.REPLANNING)
        summary = ""
        if self.snapshot:
            summary = "\n\n".join(f"# {p.path}\n{p.excerpt[:800]}" for p in self.snapshot.packets[:6])
        self.plan = run_planner(task, self.provider, summary + f"\nRecovery: {rec}", self.store)
        task.plan_version = self.plan.version
        self.store.emit(task, "RECOVERY", result=rec["action"], klass=rec["class"])
        self.store.save_task(task)

    def _stop_reason(self, task: TaskState) -> StopCondition | None:
        if task.status in {TaskStatus.ESCALATED, TaskStatus.STOPPED, TaskStatus.PR_READY}:
            return StopCondition.HUMAN_STOP
        if task.status == TaskStatus.VERIFIED:
            return StopCondition.VERIFIED_SUCCESS
        if task.iteration >= task.budget.max_iterations and task.status not in {TaskStatus.READY_TO_EXECUTE}:
            if task.status not in {TaskStatus.IMPLEMENTING}:
                return StopCondition.MAX_ITERATIONS
        if task.iteration >= task.budget.max_iterations and task.status == TaskStatus.REPLANNING:
            return StopCondition.MAX_ITERATIONS
        if task.budget.exhausted():
            return StopCondition.BUDGET_EXCEEDED
        if task.repeated_failure_count >= 2:
            return StopCondition.SAME_FAILURE_REPEATED
        if len(self.scores_history) >= 2 and self.scores_history[-1] < self.scores_history[-2] - 5:
            return StopCondition.CONFIDENCE_DECREASING
        return None

    def _next_steps(self, task: TaskState) -> list[str]:
        if task.status == TaskStatus.HUMAN_REVIEW:
            return ["Human review the diff", "Run lcc pr if approved"]
        if task.status == TaskStatus.VERIFIED:
            return ["Move to human review", "Open a pull request"]
        if task.status == TaskStatus.ESCALATED:
            return ["Resolve ambiguities or permissions", "Restart with updated issue text"]
        return ["Inspect last verification", "Continue with lcc run"]


def create_task(
    store: HarnessStore,
    task_id: str,
    objective: str,
    workspace: Path,
    issue_body: str = "",
    repository: str = "local/repo",
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
    store.save_task(task)
    store.emit(task, "TASK_RECEIVED")
    write_handoff(store, task, ["Run intake and context via lcc run"])
    return task
