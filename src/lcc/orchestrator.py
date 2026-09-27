from __future__ import annotations

import re
import time
from pathlib import Path

from lcc.agents import (
    CONTRACTS,
    intake_gate,
    needs_security,
    quick_plan,
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
from lcc.routing import maybe_route
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
from lcc.context_engine import _is_test
from lcc.tools import ToolPolicy, ToolRegistry, base_sources, changed_files, workspace_diff
from lcc.workspace import Workspace, assert_not_main

TINY_REPO_FILES = 8
TERMINAL = {TaskStatus.STOPPED, TaskStatus.ESCALATED, TaskStatus.HUMAN_REVIEW, TaskStatus.PR_READY}


class Orchestrator:
    """Owns the state machine, budgets, and agent activation. Agents never spawn agents."""

    def __init__(self, store: HarnessStore, provider: BaseProvider | None = None, *, coder_max_steps: int = 30,
                 test_timeout: int = 900) -> None:
        self.store = store
        self.raw_provider = provider or get_provider()
        self.llm: BaseProvider = self.raw_provider
        self.coder_max_steps = coder_max_steps
        self.test_timeout = test_timeout
        from lcc.config import load_config

        run_cfg = load_config().run
        self.ablate = set(run_cfg.ablate)  # e.g. {"planner", "reviewer", "intake"}
        self.mutation_mode, self.mutation_limit = run_cfg.mutation, run_cfg.mutation_limit
        self.min_speedup = run_cfg.min_speedup
        self.last_proof: dict = {}
        self.weak_test_retries = 0
        self.index = None
        self.snapshot = None
        self.rules = []
        self.applicable = []
        self.plan = None
        self.findings = []
        self.last_verification: VerificationResult | None = None
        self.last_changed: list[str] = []
        self.scope_expansions: list[str] = []
        self.failed_history: list[int] = []  # failing tests per attempt
        self.review_rejections = 0
        self.intake_score = 0.0

    # ------------------------------------------------------------ main loop
    def run(self, task: TaskState, until_human: bool = True) -> TaskState:
        started = time.monotonic()
        self.llm = MeteredProvider(maybe_route(self.raw_provider, task), self.store, task, started)
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
        self._baseline(task)
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
            if ok and task.lane != Lane.A and "reviewer" not in self.ablate:
                self._review(task)
            judge = self._judge(task, ok)
            score = global_task_score(task, self.plan, self.last_verification, self.last_changed)
            task.global_score = score["total"]
            self.failed_history.append(int(task.verification.get("tests_failed") or 0))
            self.store.save_sidecar("verification", {"score": score, "judge": judge.model_dump(mode="json")})
            passed = judge.decision == JudgeDecision.PASS and ok
            if not passed and (task.iteration >= task.budget.max_iterations or task.budget.exhausted()):
                # No attempt is left: a recovery diagnosis and a new plan would be tokens spent for nothing.
                stop = StopCondition.MAX_ITERATIONS if task.iteration >= task.budget.max_iterations else StopCondition.BUDGET_EXCEEDED
                task.stop_reason = stop.value
                transition(task, TaskStatus.FAILED)
                transition(task, TaskStatus.STOPPED)
                self.store.emit(task, "TASK_STOPPED", result="fail", reason=stop.value)
                return
            if passed:
                transition(task, TaskStatus.VERIFIED)
                sha = Workspace(Path(task.workspace)).commit(f"lcc({task.task_id}): {task.objective[:60]}")
                if self.last_proof.get("kind"):
                    from lcc.proof import make_proof

                    record = make_proof(task.base_commit, sha, self.last_proof.get("targets") or [],
                                        self.last_proof["kind"], self.last_proof["level"],
                                        task.verification.get("mutation"), task.verification.get("speed"))
                    task.verification["proof"] = record
                    self.store.write_json("proof.json", record)
                task.stop_reason = StopCondition.VERIFIED_SUCCESS.value
                self.store.emit(task, "TASK_VERIFIED", result="success", commit=sha, score=task.global_score)
                if until_human:
                    transition(task, TaskStatus.HUMAN_REVIEW)
                self.store.save_task(task)
                return
            self._recover(task, ok)
            if task.status in {TaskStatus.ESCALATED, TaskStatus.STOPPED}:
                return

    # ------------------------------------------------------------ phases
    def _intake(self, task: TaskState) -> None:
        transition(task, TaskStatus.ANALYZING)
        task.current_agent = "intake"
        self.store.save_task(task)
        if "intake" in self.ablate:  # ablation: the issue itself is the only acceptance criterion
            from lcc.schemas import AcceptanceCriterion, IntakeResult

            result = IntakeResult(problem=task.objective, intent="", requirements=[task.objective],
                                  acceptance_criteria=[AcceptanceCriterion(id="AC-01", text=task.objective)],
                                  constraints=[], ambiguities=[], blocking_ambiguities=[], risk="low", score=100)
            gate = "ok"
        else:
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
        if task.lane == Lane.A or "planner" in self.ablate:  # lane A, or ablation: the coder plans for itself
            self.plan = quick_plan(task, self.store)
        else:
            self.plan = run_planner(task, self.llm, self._summary(6, 1000), self.store)
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
        result = run_coder(task, self.llm, tools, self.plan, self.store, self.snapshot, self.applicable,
                           self.coder_max_steps, repo_map=self._coder_map())
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
        if test_res.get("timed_out"):
            test_res = self._targeted_run(task, tools, test_res)
        lint_res = self._lint_changes(task, tools)
        lint_ok = bool(lint_res.get("ok") or lint_res.get("skipped"))
        suite_ok, regress_note = self._suite_ok(task, test_res)
        ok = suite_ok and lint_ok
        proof = self._prove_fix(task, tools, test_res) if ok else None
        if proof and not proof["ok"]:
            ok = False
        mutation = self._test_strength(task, proof) if ok and proof else None
        if mutation and self.mutation_mode == "gate" and mutation["total"] >= 3 and mutation["killed"] == 0 \
                and self.weak_test_retries < 1 and task.iteration < task.budget.max_iterations:
            self.weak_test_retries += 1
            ok = False
            proof = dict(proof, message=proof["message"] + (
                f"\n[harness] Weak test: it still passes after each of {mutation['total']} deliberate breaks of your fix "
                f"({', '.join(s['op'] + ' at ' + s['file'] + ':' + str(s['line']) for s in mutation['survived'][:4])}). "
                "Assert the exact expected values so a wrong implementation fails."))
        self.last_proof = proof or {}
        evidence = [
            f"tests: rc={test_res.get('returncode')} run={test_res.get('tests_run')} failed={test_res.get('tests_failed')}",
            f"lint: ok={lint_res.get('ok')} skipped={bool(lint_res.get('skipped'))}",
        ]
        output = f"{test_res.get('stdout') or ''}\n{test_res.get('stderr') or ''}".strip()
        if regress_note:
            output += f"\n{regress_note}"
        if not test_res.get("tests_run"):
            output += "\n[harness] tests_run=0: no tests were collected, which is not a pass."
        if not lint_ok:
            output += f"\n[lint]\n{lint_res.get('stdout') or ''}{lint_res.get('stderr') or ''}"
        if proof:
            output += f"\n{proof['message']}"
            evidence.append(f"proof level={proof['level']}: {proof['message']}")
        evidence.append(output[-1500:])
        self.last_verification = VerificationResult(
            passed=ok, commands=[test_res, lint_res], acceptance=task.acceptance_criteria, evidence=evidence,
            score=100 if ok else 0,
        )
        speed = task.verification.get("speed") if task.kind == "optimize" else None
        task.verification = {
            "passed": ok,
            "tests_run": test_res.get("tests_run"),
            "tests_failed": test_res.get("tests_failed"),
            "lint_ok": lint_ok,
            "proof_level": proof["level"] if proof else 0,
            "mutation": mutation or {},
            **({"speed": speed} if speed else {}),
        }
        self.store.write_json(f"verification_{task.iteration}.json", self.last_verification)
        self.store.emit(task, "AGENT_COMPLETED", agent="verifier", result="success" if ok else "fail",
                        artifacts=[f"verification_{task.iteration}.json"], **task.verification)
        task.last_failure = None if ok else output[-6000:]
        return ok

    def _baseline(self, task: TaskState) -> None:
        """Run the suite once on the untouched branch so later runs are compared with it: tests that were
        already failing (flaky, environment-bound, unrelated) must not block verification."""
        if task.baseline:
            return
        res = self._tools(task, CONTRACTS["verifier"].permissions).run_test()
        task.baseline = {"ok": bool(res["ok"]), "tests_run": res["tests_run"], "tests_failed": res["tests_failed"],
                         "failed_ids": res.get("failed_ids") or [], "timed_out": bool(res.get("timed_out"))}
        self.store.emit(task, "BASELINE", agent="verifier", result="pass" if res["ok"] else "fail",
                        **{k: v for k, v in task.baseline.items() if k != "failed_ids"},
                        failed_ids=task.baseline["failed_ids"][:20])

    def _lint_changes(self, task: TaskState, tools: ToolRegistry) -> dict:
        """Lint only the changed Python files, and fail only on errors the change introduced: a repository with
        unrelated pre-existing lint errors must still be fixable."""
        root = Path(task.workspace)
        changed = [f for f in changed_files(root) if f.endswith(".py") and (root / f).is_file()]
        if not changed:
            return {"ok": True, "skipped": True, "reason": "no changed Python files", "diagnostics": []}
        now = tools.run_lint(changed)
        if now.get("skipped") or now.get("ok"):
            return now
        with base_sources(root, changed):
            before = tools.run_lint(changed)
        new = [d for d in now["diagnostics"] if d not in set(before.get("diagnostics") or [])]
        now["ok"] = not new
        now["stdout"] = "\n".join(f"new lint error: {d}" for d in new) if new else ""
        return now

    def _suite_ok(self, task: TaskState, res: dict) -> tuple[bool, str]:
        """Pass-to-pass: the suite is green, or every test failing now was already failing at baseline."""
        if res.get("ok"):
            return True, ""
        if not res.get("tests_run"):
            return False, ""
        now = set(res.get("failed_ids") or [])
        before = set(task.baseline.get("failed_ids") or [])
        if not now or not before:
            return False, ""  # cannot attribute failures to tests: stay strict
        new = sorted(now - before)
        if new:
            return False, f"[harness] tests failing now that passed before your change (regressions): {new[:15]}"
        return True, (f"[harness] {len(now)} test(s) still fail exactly as at baseline (pre-existing, not caused by "
                      f"this change): {sorted(now)[:10]}")

    def _targeted_run(self, task: TaskState, tools: ToolRegistry, full: dict) -> dict:
        """The full suite timed out: run the tests related to the change instead (changed tests, tests of
        changed modules) so a slow suite does not make verification impossible."""
        root = Path(task.workspace)
        changed = changed_files(root)
        stems = {Path(f).stem for f in changed if not _is_test(f)}
        targets = [f for f in changed if _is_test(f)]
        if self.index:
            targets += [t for t in self.index.tests if any(s and s in Path(t).stem for s in stems)]
        targets = list(dict.fromkeys(targets))[:8]
        if not targets:
            return full
        runs = [tools.run_test(target=t) for t in targets]
        merged = {
            "ok": all(r["ok"] for r in runs), "returncode": max(r["returncode"] for r in runs),
            "tests_run": sum(r["tests_run"] for r in runs), "tests_failed": sum(r["tests_failed"] for r in runs),
            "failed_ids": sorted({i for r in runs for i in r.get("failed_ids") or []}),
            "stdout": "\n".join(r["stdout"][-2000:] for r in runs), "timed_out": False,
            "stderr": f"[harness] full suite timed out; ran targeted tests {targets}", "cmd": "targeted",
        }
        self.store.emit(task, "DECISION", decision="full suite timed out; verified with targeted tests", targets=targets)
        return merged

    def _prove_fix(self, task: TaskState, tools: ToolRegistry, now: dict | None = None) -> dict:
        """Evidence that the change fixes something: previously failing tests now pass (fail-to-pass), or
        new/updated tests fail on the base code and pass with the change. Passing an unchanged suite is not proof."""
        root = Path(task.workspace)
        changed = changed_files(root)
        if not changed:
            return {"ok": False, "level": 0, "message": "[harness] No files were changed; there is nothing to verify."}
        before = set(task.baseline.get("failed_ids") or [])
        still = set((now or {}).get("failed_ids") or [])
        tests = [f for f in changed if _is_test(f)]
        sources = [f for f in changed if f not in tests]
        if before and before - still:
            fixed = sorted(before - still)
            return {"ok": True, "level": 5, "kind": "baseline_fixed", "targets": fixed, "sources": sources,
                    "message": f"[harness] fail-to-pass: {fixed[:10]} failed before the change and pass now."}
        if task.baseline.get("tests_failed") and not before and (now or {}).get("ok"):
            return {"ok": True, "level": 5, "kind": "baseline_fixed", "targets": tests, "sources": sources,
                    "message": f"[harness] fail-to-pass: {task.baseline['tests_failed']} test(s) failed before the change; the suite now passes."}
        if task.kind == "optimize" and (now or {}).get("tests_run"):
            speed = self._speed_proof(task, tools, sources)
            task.verification["speed"] = speed
            if not speed.get("ok"):
                return {"ok": False, "level": 1, "kind": "behavior_preserved", "targets": [], "sources": sources,
                        "message": f"[harness] optimization not proven: {speed['message']}"}
            return {"ok": True, "level": 3, "kind": "behavior_preserved", "targets": tests, "sources": sources,
                    "speed": speed, "message": (
                        f"[harness] behavior preserved: {now['tests_run']} test(s) pass with no regressions, and "
                        f"{speed['message']}")}
        if not tests:
            return {"ok": False, "level": 1, "message": (
                "[harness] The tests that pass now also passed before your change, so they prove nothing about "
                "this issue. Add or update a test that fails without your fix and passes with it.")}
        with base_sources(root, sources):
            if all(t.endswith(".py") for t in tests):
                base_results = {t: tools.run_test(target=t) for t in tests}
            else:  # other languages: the runner's file targeting varies, so run the suite on the base sources
                base_results = {"suite": tools.run_test()}
        failing_on_base = [t for t, r in base_results.items() if not r["ok"]]
        if failing_on_base:
            targets = tests if failing_on_base == ["suite"] else failing_on_base
            return {"ok": True, "level": 5, "kind": "new_tests", "targets": targets, "sources": sources,
                    "message": f"[harness] fail-to-pass: {failing_on_base} fail on the base code and pass with the change."}
        return {"ok": False, "level": 1, "message": (
            f"[harness] Your new/updated tests {tests} also pass WITHOUT your source change, so they do not "
            "demonstrate the fix. Make the test exercise the reported behavior.")}

    def _speed_proof(self, task: TaskState, tools: ToolRegistry, sources: list[str]) -> dict:
        """Time the model's benchmark (.lcc/bench.py: bench()) on the original and on the optimized sources:
        warm-up, then the median of 7 runs, in the same interpreter and sandbox as the tests."""
        root = Path(task.workspace)
        if not (root / ".lcc" / "bench.py").is_file():
            return {"ok": False, "message": "write .lcc/bench.py with a bench() function that exercises the optimized "
                                            "code on a realistic input, so the speed-up can be measured."}
        code_changed = [s for s in sources if not s.startswith(".lcc/")]
        if not code_changed:
            return {"ok": False, "message": "no source file changed, so nothing can be faster."}
        script = ("import json, runpy, statistics, time\nns = runpy.run_path('.lcc/bench.py')\nb = ns['bench']\nb()\n"
                  "ts = []\nfor _ in range(5):\n    t = time.perf_counter(); b(); ts.append(time.perf_counter() - t)\n"
                  "print('LCC_BENCH ' + json.dumps(statistics.median(ts)))")

        def timed() -> float | None:
            from lcc.tools import test_python

            res = tools._run([test_python(), "-c", script], timeout=600)
            m = re.search(r"LCC_BENCH ([0-9.eE+-]+)", res.get("stdout") or "")
            return float(m.group(1)) if m else None

        # Interleave old/new over several rounds so both see the same machine conditions: a single before/after
        # pair let timing noise on a busy CI runner pass an unchanged "optimization" as 1.1x faster.
        befores, afters = [], []
        for _ in range(3):
            with base_sources(root, code_changed):
                befores.append(timed())
            afters.append(timed())
        if None in befores or None in afters:
            return {"ok": False, "message": "the benchmark crashed on the original or the new code; bench() must run on both."}
        import statistics

        before, after = statistics.median(befores), statistics.median(afters)
        if before < 0.001:
            return {"ok": False, "message": f"the benchmark takes {before * 1000:.2f} ms: too small to measure; use a bigger input."}
        ratios = [b / a if a > 0 else float("inf") for b, a in zip(befores, afters, strict=True)]
        speedup = statistics.median(ratios)
        consistent = min(ratios) > 1.0  # the new code must win every round, not just on average
        ok = speedup >= self.min_speedup and consistent
        text = (f"{before * 1000:.1f} ms → {after * 1000:.1f} ms ({speedup:.1f}× faster, 3 interleaved rounds)" if ok else
                f"no measurable speed-up: {before * 1000:.1f} ms → {after * 1000:.1f} ms ({speedup:.2f}×"
                + ("" if consistent else ", not faster in every round") + f"; need {self.min_speedup}× every time)")
        return {"ok": ok, "before_s": round(before, 6), "after_s": round(after, 6), "speedup": round(speedup, 2),
                "message": text}

    def _test_strength(self, task: TaskState, proof: dict) -> dict | None:
        """Break the fix's added lines on purpose; the fix's tests should notice. No model call."""
        if self.mutation_mode == "off" or proof.get("level") != 5 or not proof.get("targets"):
            return None
        targets = [t for t in proof["targets"] if t.split("::")[0].endswith(".py")]
        if not targets or not any(s.endswith(".py") for s in proof.get("sources") or []):
            return None
        from lcc.proof import mutation_check

        result = mutation_check(Path(task.workspace), targets, proof["sources"], limit=self.mutation_limit)
        if result["total"]:
            self.store.emit(task, "TEST_STRENGTH", agent="verifier", result=f"{result['killed']}/{result['total']}",
                            score=result["score"], survived=[f"{s['op']} @ {s['file']}:{s['line']}" for s in result["survived"]])
        return result

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
        # A model reviewer may send a tested, proven fix back once. After that its findings are advisory: an
        # opinion must not override fail-to-pass evidence forever (false positives would burn every attempt).
        judge = run_judge(task, ok, self.findings, self.store, allow_blocking=self.review_rejections < 1)
        if judge.decision == JudgeDecision.ITERATE:
            self.review_rejections += 1
        self.store.emit(task, "AGENT_COMPLETED", agent="judge", result=judge.decision.value, blocking=len(judge.blocking_findings))
        return judge

    def _recover(self, task: TaskState, ok: bool) -> None:
        transition(task, TaskStatus.FAILED)
        transition(task, TaskStatus.DIAGNOSING)
        task.current_agent = "recovery"
        root = Path(task.workspace)
        diff = workspace_diff(root)
        if ok:  # tests passed but the judge found blocking issues
            task.last_failure = "Review blocking findings:\n" + "\n".join(
                f"- {f.description}: {f.evidence[:2]}" for f in self.findings)
        # The full verifier output: test results plus lint, regression and proof notes. The raw test command
        # alone can be green when the real reason is a missing proof or a new lint error.
        failure = {"stdout": task.last_failure or "", "stderr": ""}

        sig = _failure_signature((task.last_failure or "")[-1500:])
        prev_sig = _failure_signature(task.history[-1].new_observations[0]) if task.history and task.history[-1].new_observations else None
        task.repeated_failure_count = task.repeated_failure_count + 1 if sig and sig == prev_sig else 1
        if task.repeated_failure_count >= 2:  # same failure twice: another diagnosis would repeat itself
            task.history.append(IterationRecord(
                iteration=task.iteration, previous_state=TaskStatus.JUDGING.value,
                new_observations=[(task.last_failure or "")[-1500:]], changed_files=list(self.last_changed)))
            task.stop_reason = StopCondition.SAME_FAILURE_REPEATED.value
            transition(task, TaskStatus.STOPPED)
            self.store.emit(task, "TASK_STOPPED", result="fail", reason=task.stop_reason)
            self.store.save_task(task)
            return
        rec = run_recovery(task, failure, diff, self.llm, self.store, self.findings)

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
        if task.lane == Lane.A or "planner" in self.ablate:
            self.plan = quick_plan(task, self.store, recovery_note=note)
        else:
            self.plan = run_planner(task, self.llm, self._summary(4, 800), self.store, recovery_note=note)
        task.plan_version = self.plan.version
        record.new_plan = [s.action for s in self.plan.steps]
        self.store.write_json(f"iteration_{task.iteration}.json", record)
        self.store.save_task(task)

    # ------------------------------------------------------------ helpers
    def _tools(self, task: TaskState, permissions: dict[str, bool], allowed=None, forbidden=None) -> ToolRegistry:
        policy = ToolPolicy(permissions, allowed_paths=allowed, forbidden_paths=forbidden)
        return ToolRegistry(Path(task.workspace), policy, task.budget, index=self.index, test_timeout=self.test_timeout)

    def _coder_map(self) -> str:
        """A compact repo map in the coder prefix saves an orientation step on repos larger than the snapshot."""
        if not self.index or not self.snapshot or len(self.index.files) <= len(self.snapshot.files) + 4:
            return ""
        return repo_map(self.index, token_budget=500)

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
        h = self.failed_history
        if len(h) >= 3 and h[-1] > h[-2] > h[-3]:  # every attempt breaks more tests: stop digging
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
    """Normalize test output so timings and addresses don't make identical failures look different. Output
    without any failure keyword (lint errors, custom runners) falls back to its last non-empty lines."""
    lines = [line for line in text.splitlines() if line.strip()]
    keep = [line for line in lines if re.search(r"(FAIL|ERROR|Error|error|assert|failed|\[harness\])", line)]
    sig = "\n".join((keep or lines)[-12:])
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
    kind: str = "bug",
) -> TaskState:
    store.init_layout()
    task = TaskState(
        task_id=task_id,
        repository=repository,
        workspace=str(Path(workspace).resolve()),
        objective=objective,
        issue_body=issue_body,
        branch=f"agent/{task_id}",
        kind=kind,
    )
    for k, v in (budget_overrides or {}).items():
        setattr(task.budget, k, v)
    store.save_task(task)
    store.emit(task, "TASK_RECEIVED")
    write_handoff(store, task, ["Run intake and context via lcc run"])
    return task
