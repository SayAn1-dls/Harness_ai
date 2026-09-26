from __future__ import annotations

from pathlib import Path
from typing import Any

from lcc.agent_loop import run_tool_loop, truncate
from lcc.constants import INTAKE_HIGH_RISK_SCORE, INTAKE_PASS_SCORE, MAX_AGENT_DEPTH
from lcc.context_engine import RepoIndex
from lcc.model import BaseProvider
from lcc.schemas import (
    AcceptanceCriterion,
    AgentContract,
    AgentResult,
    ContextSnapshot,
    EvidenceLevel,
    FailureClass,
    Finding,
    FindingCategory,
    ImplementationPlan,
    IntakeResult,
    JudgeDecision,
    JudgeResult,
    PlanStep,
    RecoveryAction,
    Rule,
    Severity,
    TaskState,
)
from lcc.store import HarnessStore
from lcc.tools import ToolRegistry, changed_files

ISSUE_CHARS = 8000  # long GitHub threads are cut here; the head and the tail carry most of the signal


def issue_text(task: TaskState, limit: int = ISSUE_CHARS) -> str:
    return truncate(task.issue_body or task.objective, limit)


NONE_WORDS = {"", "none", "n/a", "na", "no", "nothing", "null", "-", "[]", "none.", "no ambiguities"}


def as_list(value: Any) -> list:
    """Models return a bare string, null or "none" where a list is expected; never iterate a string by character."""
    if value is None:
        return []
    if isinstance(value, str):
        return [] if value.strip().lower() in NONE_WORDS else [value]
    if isinstance(value, dict):
        return [value]
    try:
        return [v for v in value if not (isinstance(v, str) and v.strip().lower() in NONE_WORDS)]
    except TypeError:
        return [value]


def as_float(value: Any, default: float) -> float:
    words = {"very high": 0.95, "high": 0.85, "medium": 0.6, "moderate": 0.6, "low": 0.3, "very low": 0.1}
    if isinstance(value, str) and value.strip().lower() in words:
        return words[value.strip().lower()]
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

CONTRACTS: dict[str, AgentContract] = {
    "intake": AgentContract(
        name="intake",
        purpose="Convert a raw issue into a structured engineering problem.",
        outputs=["intake.json"],
        permissions={"read_repository": False, "write_repository": False, "shell": False},
        success_conditions=["requirements extracted", "blocking ambiguities surfaced"],
    ),
    "mapper": AgentContract(
        name="mapper",
        purpose="Build repository map, symbols, and tests.",
        tools=["repo_tree"],
        permissions={"read_repository": True},
    ),
    "context": AgentContract(
        name="context",
        purpose="When the context snapshot scores below the gate, explore read-only and name the missing files.",
        tools=["get_repo_map", "search_code", "find_symbol", "find_references", "read_file", "repo_tree"],
        permissions={"read_repository": True, "write_repository": False, "shell": False},
        max_tool_calls=12,
    ),
    "rules": AgentContract(
        name="rules",
        purpose="Discover, scope, and prioritize engineering rules.",
        permissions={"read_repository": True},
    ),
    "impact": AgentContract(
        name="impact",
        purpose="Identify direct and indirect effects of a proposed change.",
        tools=["find_references"],
        permissions={"read_repository": True, "write_repository": False},
        max_tool_calls=20,
        success_conditions=["affected_surface_identified", "confidence >= 0.80"],
    ),
    "planner": AgentContract(
        name="planner",
        purpose="Create an implementation plan. Cannot modify code.",
        permissions={"read_repository": True, "write_repository": False, "shell": False},
    ),
    "coder": AgentContract(
        name="coder",
        purpose="Implement the plan with a minimal patch.",
        tools=[
            "search_code", "find_symbol", "read_file", "repo_tree",
            "edit_file", "write_file", "run_test", "shell", "git_history",
        ],
        permissions={"read_repository": True, "write_repository": True, "run_tests": True, "shell": True},
        max_tool_calls=40,
    ),
    "verifier": AgentContract(
        name="verifier",
        purpose="Independently execute tests and static checks.",
        tools=["run_test", "run_lint", "run_typecheck"],
        permissions={"read_repository": True, "run_tests": True},
    ),
    "security": AgentContract(
        name="security",
        purpose="Risk-triggered security review.",
        permissions={"read_repository": True, "write_repository": False},
    ),
    "reviewer": AgentContract(
        name="reviewer",
        purpose="Adversarially try to prove the change is wrong.",
        permissions={"read_repository": True, "write_repository": False},
    ),
    "judge": AgentContract(
        name="judge",
        purpose="Reconcile findings and decide PASS/FAIL.",
        permissions={"read_repository": True, "write_repository": False},
    ),
    "recovery": AgentContract(
        name="recovery",
        purpose="Classify failures and choose a recovery action.",
        permissions={"read_repository": True, "write_repository": False},
    ),
}

HARNESS_RULES = """Rules: work only on this task branch; never touch harness/ or .git/ (the harness owns git). Make the
smallest change that meets the acceptance criteria: no refactors, renames or reformatting. An independent verifier
runs the tests and checks that your new test fails without your fix."""


class AgentSpawnError(RuntimeError):
    pass


class AgentHost:
    """Agents cannot spawn agents. Depth is enforced here."""

    def __init__(self, depth: int = 0) -> None:
        self.depth = depth

    def child(self) -> AgentHost:
        if self.depth + 1 > MAX_AGENT_DEPTH:
            raise AgentSpawnError("MAX_AGENT_DEPTH exceeded; orchestrator owns spawning")
        return AgentHost(self.depth + 1)


# ---------------------------------------------------------------- intake
INTAKE_SCHEMA = (
    '{"problem":str,"intent":str,"requirements":[str],"acceptance_criteria":[{"id":str,"text":str}],'
    '"constraints":[str],"ambiguities":[str],"blocking_ambiguities":[str],"risk":"low|medium|high|critical"}'
)


def run_intake(task: TaskState, provider: BaseProvider, store: HarnessStore, critique: str = "") -> IntakeResult:
    prompt = f"Issue title: {task.objective}\n\nIssue body:\n{issue_text(task)}\n"
    if critique:
        prompt += f"\nYour previous analysis was incomplete: {critique}\nFix those gaps."
    data = provider.structured_output(
        prompt,
        system=(
            "You are the Issue Analyst of an autonomous coding harness. Turn the issue into a structured, testable "
            "engineering problem. acceptance_criteria must be concrete and checkable. `ambiguities` lists open "
            "questions you can resolve with a reasonable default (state the default). `blocking_ambiguities` lists "
            "ONLY questions where any implementation would be a guess about what the requester wants (e.g. no "
            "observable success condition at all); leave it empty if a competent engineer could proceed. "
            "risk is high/critical only for auth, payments, data loss, security or wide-reaching changes."
        ),
        schema_hint=INTAKE_SCHEMA,
        agent="intake",
    )
    criteria = []
    for i, item in enumerate(as_list(data.get("acceptance_criteria")), 1):
        if isinstance(item, str):
            criteria.append(AcceptanceCriterion(id=f"AC-{i:02d}", text=item))
        elif isinstance(item, dict):
            criteria.append(AcceptanceCriterion(id=str(item.get("id") or f"AC-{i:02d}"), text=str(item.get("text") or item)))
    reqs = [str(x) for x in as_list(data.get("requirements"))]
    ambiguities = [str(x) for x in as_list(data.get("ambiguities"))]
    result = IntakeResult(
        problem=str(data.get("problem") or task.objective),
        intent=str(data.get("intent") or ""),
        requirements=reqs or [task.objective],
        acceptance_criteria=criteria or [AcceptanceCriterion(id="AC-01", text="The issue is resolved and tests pass")],
        constraints=[str(x) for x in as_list(data.get("constraints"))],
        ambiguities=ambiguities,
        blocking_ambiguities=[str(x) for x in as_list(data.get("blocking_ambiguities"))],
        risk=str(data.get("risk") or "low").lower() if str(data.get("risk") or "low").lower() in {"low", "medium", "high", "critical"} else "medium",
        score=_intake_score(data, criteria, reqs),
    )
    store.write_json("intake.json", result)
    return result


def _intake_score(data: dict[str, Any], criteria: list[AcceptanceCriterion], reqs: list[str]) -> float:
    req = 25 if reqs else 10
    intent = 20 if data.get("intent") else 8
    ac = 20 if criteria else 0
    amb = 15 if "ambiguities" in data else 8
    cons = 10 if data.get("constraints") is not None else 5
    scope = 10 if data.get("problem") else 4
    return req + intent + ac + amb + cons + scope


def intake_gate(result: IntakeResult) -> str:
    if result.blocking_ambiguities:
        return "escalate"
    if result.risk in {"high", "critical"} and result.score < INTAKE_HIGH_RISK_SCORE:
        return "escalate"
    if result.score < INTAKE_PASS_SCORE:
        return "iterate_intake"
    return "ok"


# ---------------------------------------------------------------- context
def run_context_agent(task: TaskState, provider: BaseProvider, tools: ToolRegistry, snapshot: ContextSnapshot) -> list[str]:
    """Read-only exploration when the snapshot is below the gate. Returns extra files to include."""
    loop = run_tool_loop(
        provider,
        agent="context",
        system=(
            "You are the Context agent. The retrieved context for this task scored too low. Explore the repository "
            "read-only and find the files an engineer would need to implement the task (the code to change, its "
            "callers, and the relevant tests). Be economical. Then call finish with `files` = those paths."
        ),
        prompt=f"Task: {task.objective}\n\nIssue:\n{issue_text(task, 4000)}\n\nAlready selected: {snapshot.files}\n"
        f"Score breakdown: {snapshot.scores}",
        tools=tools,
        allowed=CONTRACTS["context"].tools,
        budget=task.budget,
        max_steps=CONTRACTS["context"].max_tool_calls,
    )
    return [str(f) for f in as_list(loop.finish_args.get("files")) if isinstance(f, str)]


# ---------------------------------------------------------------- impact
def run_impact(index: RepoIndex, task: TaskState, files: list[str]) -> dict[str, Any]:
    affected = list(dict.fromkeys(files))
    potential: list[str] = []
    callers: dict[str, list[str]] = {}
    for f in affected:
        c = sorted(index.reverse_graph.get(f, set()))
        callers[f] = c
        potential.extend(c)
        potential.extend(sorted(index.graph.get(f, set())))
    tests = [t for t in index.tests if any(Path(a).stem in t or Path(a).name in t for a in affected)]
    if not tests:
        tests = index.tests[:8]
    return {
        "affected_files": affected,
        "potentially_affected_files": sorted(set(potential) - set(affected)),
        "callers": callers,
        "tests": tests,
        "regression_risks": [f"Callers of {k}: {v}" for k, v in callers.items() if v],
        "score": 80 if affected else 40,
    }


# ---------------------------------------------------------------- planner
def run_planner(
    task: TaskState,
    provider: BaseProvider,
    context_summary: str,
    store: HarnessStore,
    recovery_note: str = "",
) -> ImplementationPlan:
    prompt = (
        f"Objective: {task.objective}\nIssue:\n{issue_text(task, 5000)}\n"
        f"Acceptance criteria: {[c.text for c in task.acceptance_criteria]}\n"
        f"Candidate files: {task.affected_files}\n\nContext:\n{context_summary[:12000]}"
    )
    if task.history:
        prompt += "\n\nPrevious attempts:\n" + _history_text(task)
    if recovery_note:
        prompt += f"\n\nRecovery diagnosis for the last failure:\n{recovery_note}"
    data = provider.structured_output(
        prompt,
        system=(
            "You are the Planner. You cannot modify code. Produce a short ordered plan (2-6 steps) naming the exact "
            "files to change and how each step is verified. allowed_files = files the coder may edit, including "
            "test files. If previous attempts failed, the new plan must address why."
        ),
        schema_hint='{"steps":[{"order":int,"action":str,"files":[str],"verification":str}],"allowed_files":[str],"forbidden_files":[str]}',
        agent="planner",
    )
    steps = []
    for i, s in enumerate(as_list(data.get("steps")), 1):
        if isinstance(s, dict):
            steps.append(
                PlanStep(
                    order=as_int(s.get("order"), i),
                    action=str(s.get("action") or s),
                    files=[str(f) for f in as_list(s.get("files"))],
                    verification=str(s.get("verification") or "tests"),
                )
            )
        else:
            steps.append(PlanStep(order=i, action=str(s)))
    if not steps:
        steps = [
            PlanStep(order=1, action="Implement the objective with a minimal patch", files=task.affected_files),
            PlanStep(order=2, action="Add or update a regression test", verification="tests"),
        ]
    allowed = [str(f) for f in as_list(data.get("allowed_files"))] or sorted({f for s in steps for f in s.files})
    forbidden = [str(f) for f in as_list(data.get("forbidden_files"))]
    plan = ImplementationPlan(version=task.plan_version + 1, steps=steps, allowed_files=allowed, forbidden_files=forbidden)
    store.write_json(f"plan_v{plan.version}.json", plan)
    store.write_json("plan.json", plan)
    return plan


def quick_plan(task: TaskState, store: HarnessStore, recovery_note: str = "") -> ImplementationPlan:
    """Deterministic plan for lane A: no model call; the coder's own workflow covers the planning."""
    steps = []
    if recovery_note:
        steps.append(PlanStep(order=1, action=f"Address the last failure first. {recovery_note}", files=[]))
    steps += [
        PlanStep(order=len(steps) + 1, action="Locate the root cause and fix it minimally", files=task.affected_files[:6]),
        PlanStep(order=len(steps) + 2, action="Add a regression test that fails before the fix", verification="tests"),
    ]
    plan = ImplementationPlan(version=task.plan_version + 1, steps=steps, allowed_files=[], forbidden_files=[])
    store.write_json(f"plan_v{plan.version}.json", plan)
    store.write_json("plan.json", plan)
    return plan


# ---------------------------------------------------------------- coder
CODER_SYSTEM = """You are the Implementation Agent of an autonomous coding harness. Fix the issue like a careful senior
engineer: find the root cause, make a minimal correct change, prove it with a test.
1. Locate: use the context excerpts below first; then find_symbol / search_code (regex \bname\b finds usages) / read_file line ranges.
2. Reproduce when cheap: run_test on one file, or `shell` with python -c.
3. Fix the root cause, including the edge cases the issue implies. Keep public signatures; check callers.
4. Add a regression test beside the existing tests, in their style, that fails before the fix; run_test it.
5. Call finish (root cause + fix, two sentences) as soon as tests pass.
Every step re-sends this conversation: batch independent lookups as several tool calls in one turn, read line
ranges not whole files, and do not re-read a file you just edited (edit_file returns the new lines)."""


SNAPSHOT_CHARS = 14_000  # total excerpt budget in the coder prefix (~3.5k tokens), re-sent on every step
PACKET_CHARS = 4_000


def coder_system_prompt(rules: list[Rule], snapshot: ContextSnapshot | None, repo_map: str = "") -> str:
    """Static prefix: identical across steps and iterations while the snapshot is unchanged, so providers'
    prefix caches (DeepSeek, DashScope) hit on every call."""
    parts = [CODER_SYSTEM, HARNESS_RULES]
    if rules:
        parts.append("Repository rules:\n" + "\n".join(f"- [{r.id} {r.scope}] {r.instruction[:400]}" for r in rules[:10]))
    if repo_map:
        parts.append(truncate(repo_map, 2500))
    if snapshot and snapshot.packets:
        used, blocks = 0, []
        for p in snapshot.packets[:8]:
            room = min(PACKET_CHARS, SNAPSHOT_CHARS - used)
            if room < 400:
                break
            excerpt = p.excerpt if len(p.excerpt) <= room else p.excerpt[:room] + "\n[... excerpt cut; read_file for the rest]"
            blocks.append(f"### {p.path}\n```\n{excerpt}\n```")
            used += len(excerpt)
        parts.append(f"Context snapshot {snapshot.snapshot_id} (ranked excerpts; read_file gives numbered lines):\n" + "\n\n".join(blocks))
    return "\n\n".join(parts)


def coder_task_prompt(task: TaskState, plan: ImplementationPlan, diff: str) -> str:
    """Dynamic suffix: plan, previous attempts, last failure, current diff."""
    lines = [
        f"Task {task.task_id}: {task.objective}",
        f"Issue:\n{issue_text(task)}",
        "Acceptance criteria:\n" + "\n".join(f"- {c.id}: {c.text}" for c in task.acceptance_criteria),
        f"Plan v{plan.version}:\n" + "\n".join(f"{s.order}. {s.action} {s.files or ''}" for s in plan.steps),
        f"Allowed files: {plan.allowed_files or 'any (keep it minimal)'}",
    ]
    if task.baseline:
        b = task.baseline
        line = f"Baseline before any change: tests_run={b.get('tests_run')} failed={b.get('tests_failed')}."
        if b.get("failed_ids"):
            line += f" Failing at baseline: {b['failed_ids'][:10]} (if unrelated to the issue, leave them alone)."
        if b.get("timed_out"):
            line += " The full suite is slow: run focused test files with run_test(target=...)."
        if task.kind == "optimize":
            line += (" This is a behavior-preserving optimization: results must not change and every existing test"
                     " must keep passing. Add a test only if nothing covers the code you change.")
        elif not b.get("tests_failed"):
            line += (" The existing suite does not catch this issue, so you MUST add or update a test that fails"
                     " without your fix and passes with it; the verifier checks this.")
        lines.append(line)
    if task.history:
        lines.append(f"This is attempt {task.iteration}. Previous attempts (do not repeat them):\n" + _history_text(task))
    if task.last_failure and task.iteration > 1:
        lines.append(f"Latest verification failure output:\n```\n{task.last_failure[-3000:]}\n```")
    if diff.strip():
        lines.append(f"Current uncommitted diff (your earlier work is still applied):\n```diff\n{truncate(diff, 5000)}\n```")
    lines.append("Work through the steps: locate, reproduce, fix the root cause, add a regression test, run it, then call finish.")
    return "\n\n".join(lines)


def run_coder(
    task: TaskState,
    provider: BaseProvider,
    tools: ToolRegistry,
    plan: ImplementationPlan,
    store: HarnessStore,
    snapshot: ContextSnapshot | None,
    rules: list[Rule],
    max_steps: int = 30,
    repo_map: str = "",
) -> AgentResult:
    diff = tools.git_diff().get("diff", "")
    if diff.startswith("(no changes"):
        diff = ""
    loop = run_tool_loop(
        provider,
        agent="coder",
        system=coder_system_prompt(rules, snapshot, repo_map),
        prompt=coder_task_prompt(task, plan, diff),
        tools=tools,
        allowed=CONTRACTS["coder"].tools,
        budget=task.budget,
        max_steps=max_steps,
    )
    for rel in changed_files(tools.workspace):  # edits made through `shell` count too
        if rel not in tools.changed:
            tools.changed.append(rel)
    data = {
        "changed": list(tools.changed),
        "scope_expansions": list(tools.scope_expansions),
        "summary": loop.summary,
        "stop_reason": loop.stop_reason,
        "steps": loop.steps,
        "tool_calls": loop.tool_calls,
    }
    store.write_json(f"coder_{task.iteration}.json", data)
    store.write_json(f"coder_{task.iteration}_transcript.json", loop.messages)
    return AgentResult(agent="coder", success=loop.finished and bool(tools.changed), summary=loop.summary, data=data)


def _history_text(task: TaskState) -> str:
    out = []
    for rec in task.history[-4:]:
        out.append(
            f"- attempt {rec.iteration}: changed {rec.changed_files}; failure class {rec.failure_class.value if rec.failure_class else '?'}; "
            f"action {rec.recovery_action.value if rec.recovery_action else '?'}; "
            f"observations: {'; '.join(o[:400] for o in rec.new_observations)}"
        )
    return "\n".join(out)


# ---------------------------------------------------------------- review
def run_reviewer(
    task: TaskState,
    provider: BaseProvider,
    diff: str,
    store: HarnessStore,
    test_evidence: str = "",
    scope_expansions: list[str] | None = None,
) -> list[Finding]:
    data = provider.structured_output(
        f"Task: {task.objective}\nIssue:\n{issue_text(task, 4000)}\n"
        f"Acceptance: {[c.model_dump(include={'id', 'text'}) for c in task.acceptance_criteria]}\n"
        f"Files edited outside the plan: {scope_expansions or []}\n"
        f"Test evidence:\n{test_evidence[-800:]}\n\nDiff:\n{truncate(diff, 12000)}",
        system=(
            "You are an adversarial reviewer. Try to prove the change is wrong: unmet acceptance criteria, broken "
            "callers, missing edge cases, unjustified scope. Report only real problems, each with concrete evidence "
            "quoted from the diff. Tests already pass, so do not report style nits. Empty findings is a valid answer."
        ),
        schema_hint='{"findings":[{"category":"CORRECTNESS|SECURITY|REQUIREMENTS|RULES|TESTING|RELIABILITY|COMPATIBILITY",'
        '"severity":"BLOCKER|CRITICAL|HIGH|MEDIUM|LOW|INFO","confidence":float,"description":str,"evidence":[str],"file":str}]}',
        agent="reviewer",
    )
    findings: list[Finding] = []
    for i, f in enumerate(as_list(data.get("findings")), 1):
        if not isinstance(f, dict):
            continue
        evidence = [str(e) for e in as_list(f.get("evidence"))]
        confidence = as_float(f.get("confidence"), 0.5)
        if not evidence:
            confidence = min(confidence, 0.5)  # no evidence, no high-confidence finding
        try:
            cat = FindingCategory(str(f.get("category") or "CORRECTNESS").upper())
        except ValueError:
            cat = FindingCategory.CORRECTNESS
        try:
            sev = Severity(str(f.get("severity") or "MEDIUM").upper())
        except ValueError:
            sev = Severity.MEDIUM
        findings.append(
            Finding(
                finding_id=f"F-{task.task_id}-{task.iteration}-{i:03d}",
                category=cat,
                severity=sev,
                confidence=confidence,
                file=f.get("file"),
                description=str(f.get("description") or ""),
                evidence=evidence,
                agent="reviewer",
                iteration=task.iteration,
            )
        )
    store.write_json(f"review_{task.iteration}.json", {"findings": [x.model_dump() for x in findings]})
    return findings


SECURITY_NEEDLES = {
    "eval(": "Dynamic eval of data",
    "exec(": "Dynamic exec of data",
    "pickle.loads": "Unsafe deserialization",
    "yaml.load(": "Unsafe YAML load (use safe_load)",
    "shell=True": "Command injection risk",
    "os.system(": "Command injection risk",
    "innerHTML": "XSS risk",
    "verify=False": "TLS verification disabled",
}


def run_security(task: TaskState, diff: str, store: HarnessStore) -> list[Finding]:
    added = [line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
    findings: list[Finding] = []
    for needle, desc in SECURITY_NEEDLES.items():
        hits = [line.strip() for line in added if needle in line]
        if hits:
            findings.append(
                Finding(
                    finding_id=f"S-{task.task_id}-{task.iteration}-{len(findings) + 1:03d}",
                    category=FindingCategory.SECURITY,
                    severity=Severity.HIGH,
                    confidence=0.75,
                    description=desc,
                    evidence=[f"added line: {h[:200]}" for h in hits[:3]],
                    evidence_level=EvidenceLevel.RULE_VIOLATION,
                    agent="security",
                    iteration=task.iteration,
                )
            )
    store.write_json(f"security_{task.iteration}.json", {"findings": [x.model_dump() for x in findings]})
    return findings


def run_judge(task: TaskState, verification_ok: bool, findings: list[Finding], store: HarnessStore,
              allow_blocking: bool = True) -> JudgeResult:
    blocking = [
        f
        for f in findings
        if f.severity in {Severity.BLOCKER, Severity.CRITICAL, Severity.HIGH} and f.confidence >= 0.7 and f.evidence
        and (allow_blocking or f.agent == "security")
    ]
    nonblocking = [f for f in findings if f not in blocking]
    if not verification_ok:
        decision, conf = JudgeDecision.FAIL, 0.85
    elif blocking:
        decision, conf = JudgeDecision.ITERATE, 0.7
    else:
        decision, conf = JudgeDecision.PASS, 0.9 if not nonblocking else 0.8
    result = JudgeResult(
        decision=decision,
        confidence=conf,
        blocking_findings=blocking,
        non_blocking_findings=nonblocking,
        evidence=[f"verification_ok={verification_ok}", f"blocking={len(blocking)}"],
        remaining_risk=[b.description for b in blocking],
    )
    store.write_json(f"judge_{task.iteration}.json", result)
    store.write_json("judge.json", result)
    return result


# ---------------------------------------------------------------- recovery
def classify_failure(blob: str) -> tuple[FailureClass, RecoveryAction]:
    b = blob.lower()
    if "modulenotfounderror" in b or "importerror" in b:
        return FailureClass.DEPENDENCY_BUG, RecoveryAction.RESEARCH
    if "permission denied" in b or "eacces" in b:
        return FailureClass.ENVIRONMENT_BUG, RecoveryAction.ESCALATE
    if "syntaxerror" in b or "indentationerror" in b:
        return FailureClass.CODE_BUG, RecoveryAction.PATCH
    if "no tests ran" in b or "tests_run=0" in b:
        return FailureClass.TEST_BUG, RecoveryAction.PATCH
    if "assertionerror" in b or "failed" in b or "error" in b:
        return FailureClass.CODE_BUG, RecoveryAction.PATCH
    return FailureClass.UNKNOWN, RecoveryAction.REPLAN


def run_recovery(
    task: TaskState,
    failure: dict[str, Any],
    diff: str,
    provider: BaseProvider,
    store: HarnessStore,
    findings: list[Finding] | None = None,
) -> dict[str, Any]:
    output = f"{failure.get('stdout') or ''}\n{failure.get('stderr') or ''}".strip()
    cls, action = classify_failure(output)
    blocking = [f"{f.severity.value}: {f.description} | {f.evidence[:2]}" for f in findings or []]
    data = provider.structured_output(
        f"Task: {task.objective}\nAttempt {task.iteration} failed.\n"
        f"Verification output (tail):\n{output[-4000:]}\n\nBlocking review findings: {blocking}\n\n"
        f"Diff of the attempt:\n{truncate(diff, 5000)}\n\nPrevious attempts:\n{_history_text(task) or 'none'}\n\n"
        f"Heuristic guess: class={cls.value} action={action.value}",
        system=(
            "You are the Recovery Agent. Diagnose WHY the attempt failed (which assumption was wrong), classify it, "
            "and choose the next action. Classes: CODE_BUG, TEST_BUG, ENVIRONMENT_BUG, DEPENDENCY_BUG, "
            "WRONG_ASSUMPTION, MISSING_CONTEXT, TOOL_FAILURE, UNKNOWN. Actions: patch (fix forward), research "
            "(need more context), replan, rollback (discard the attempt and start clean), retry, escalate (only if "
            "a human is required). `failed_assumption` is one sentence; `guidance` tells the coder what to do "
            "differently; `files_to_inspect` lists paths worth reading."
        ),
        schema_hint='{"class":str,"action":str,"failed_assumption":str,"guidance":str,"files_to_inspect":[str]}',
        agent="recovery",
    )
    try:
        cls = FailureClass(str(data.get("class") or cls.value).upper())
    except ValueError:
        pass
    try:
        raw_action = str(data.get("action") or action.value).lower().strip()
        action = RecoveryAction({"replan": "re-plan", "re_plan": "re-plan"}.get(raw_action, raw_action))
    except ValueError:
        pass
    out = {
        "class": cls.value,
        "action": action.value,
        "failed_assumption": str(data.get("failed_assumption") or ""),
        "guidance": str(data.get("guidance") or data.get("notes") or ""),
        "files_to_inspect": [str(f) for f in as_list(data.get("files_to_inspect"))],
    }
    store.write_json(f"recovery_{task.iteration}.json", out)
    return out


SECURITY_KEYWORDS = (
    "auth", "password", "token", "payment", "secret", "session", "permission", "xss", "inject", "traversal",
    "sanitiz", "escape", "csrf", "ssrf", "credential", "encrypt", "upload", "sql",
)


def needs_security(task: TaskState) -> bool:
    blob = f"{task.objective} {task.issue_body} {task.risk}".lower()
    return task.risk in {"high", "critical"} or any(k in blob for k in SECURITY_KEYWORDS)
