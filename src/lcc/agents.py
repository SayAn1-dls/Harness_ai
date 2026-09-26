from __future__ import annotations

from pathlib import Path
from typing import Any

from lcc.constants import INTAKE_HIGH_RISK_SCORE, INTAKE_PASS_SCORE, MAX_AGENT_DEPTH
from lcc.context_engine import RepoIndex
from lcc.model import ModelProvider
from lcc.schemas import (
    AcceptanceCriterion,
    AgentContract,
    AgentResult,
    FailureClass,
    Finding,
    FindingCategory,
    ImplementationPlan,
    IntakeResult,
    JudgeDecision,
    JudgeResult,
    PlanStep,
    RecoveryAction,
    Severity,
    TaskState,
)
from lcc.store import HarnessStore
from lcc.tools import ToolRegistry

CONTRACTS: dict[str, AgentContract] = {
    "intake": AgentContract(
        name="intake",
        purpose="Convert a raw issue into a structured engineering problem.",
        outputs=["intake.json"],
        tools=["search_code", "read_file"],
        permissions={"read_repository": True, "write_repository": False, "shell": False},
        success_conditions=["requirements extracted"],
    ),
    "mapper": AgentContract(
        name="mapper",
        purpose="Build repository map, symbols, and tests.",
        tools=["repo_tree"],
        permissions={"read_repository": True},
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
        tools=["read_file", "search_code", "write_file", "apply_patch", "git_diff"],
        permissions={"read_repository": True, "write_repository": True, "shell": False},
        max_tool_calls=40,
    ),
    "verifier": AgentContract(
        name="verifier",
        purpose="Independently execute tests and static checks.",
        tools=["run_test", "run_lint", "run_typecheck"],
        permissions={"read_repository": True, "shell": True},
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


def run_intake(task: TaskState, provider: ModelProvider, store: HarnessStore) -> IntakeResult:
    data = provider.structured_output(
        f"Issue:\n{task.issue_body or task.objective}\n",
        system="You are the Issue Analyst. Extract problem, intent, requirements, acceptance criteria, constraints, ambiguities, and risk.",
        schema_hint='{"problem":str,"intent":str,"requirements":[str],"acceptance_criteria":[{"id":str,"text":str}],"constraints":[str],"ambiguities":[str],"risk":str}',
    )
    criteria = []
    for i, item in enumerate(data.get("acceptance_criteria") or [], 1):
        if isinstance(item, str):
            criteria.append(AcceptanceCriterion(id=f"AC-{i:02d}", text=item))
        elif isinstance(item, dict):
            criteria.append(
                AcceptanceCriterion(id=str(item.get("id") or f"AC-{i:02d}"), text=str(item.get("text") or item))
            )
    if not criteria:
        criteria = [AcceptanceCriterion(id="AC-01", text="The issue is resolved and tests pass")]
    reqs = [str(x) for x in data.get("requirements") or [task.objective]]
    ambiguities = [str(x) for x in data.get("ambiguities") or []]
    score = _intake_score(data, criteria, ambiguities)
    result = IntakeResult(
        problem=str(data.get("problem") or task.objective),
        intent=str(data.get("intent") or task.objective),
        requirements=reqs,
        acceptance_criteria=criteria,
        constraints=[str(x) for x in data.get("constraints") or []],
        ambiguities=ambiguities,
        risk=str(data.get("risk") or "low"),
        score=score,
    )
    store.write_json("intake.json", result)
    return result


def _intake_score(data: dict[str, Any], criteria: list[AcceptanceCriterion], ambiguities: list[str]) -> float:
    req = 25 if data.get("requirements") else 10
    intent = 20 if data.get("intent") else 8
    ac = 20 if criteria else 0
    amb = 15 if "ambiguities" in data else 8
    cons = 10 if data.get("constraints") is not None else 5
    scope = 10 if data.get("problem") else 4
    return req + intent + ac + amb + cons + scope


def intake_gate(result: IntakeResult) -> str:
    if result.risk in {"high", "critical"} and result.score < INTAKE_HIGH_RISK_SCORE:
        return "escalate"
    if result.score < INTAKE_PASS_SCORE:
        return "iterate_intake"
    return "ok"


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
    report = {
        "affected_files": affected,
        "potentially_affected_files": sorted(set(potential) - set(affected)),
        "callers": callers,
        "tests": tests,
        "regression_risks": [f"Callers of {k}: {v}" for k, v in callers.items() if v],
        "score": 80 if affected else 40,
    }
    return report


def run_planner(task: TaskState, provider: ModelProvider, context_summary: str, store: HarnessStore) -> ImplementationPlan:
    data = provider.structured_output(
        f"Objective: {task.objective}\nAcceptance: {[c.text for c in task.acceptance_criteria]}\n"
        f"Affected: {task.affected_files}\nContext:\n{context_summary[:6000]}",
        system="You are the Planner. You cannot modify code. Produce ordered implementation steps, allowed files, and verification.",
        schema_hint='{"steps":[{"order":int,"action":str,"files":[str],"verification":str}],"allowed_files":[str],"forbidden_files":[str]}',
    )
    steps = []
    for i, s in enumerate(data.get("steps") or [], 1):
        if isinstance(s, dict):
            steps.append(
                PlanStep(
                    order=int(s.get("order") or i),
                    action=str(s.get("action") or s),
                    files=list(s.get("files") or []),
                    verification=str(s.get("verification") or "tests"),
                )
            )
        else:
            steps.append(PlanStep(order=i, action=str(s)))
    if not steps:
        steps = [
            PlanStep(order=1, action="Implement the objective with a minimal patch", files=task.affected_files),
            PlanStep(order=2, action="Update or add tests", verification="pytest"),
        ]
    allowed = list(data.get("allowed_files") or task.affected_files)
    forbidden = list(data.get("forbidden_files") or ["harness/state/task_state.json"])
    plan = ImplementationPlan(version=task.plan_version + 1, steps=steps, allowed_files=allowed, forbidden_files=forbidden)
    store.write_json("plan.json", plan)
    return plan


def run_coder(
    task: TaskState,
    provider: ModelProvider,
    tools: ToolRegistry,
    plan: ImplementationPlan,
    store: HarnessStore,
    context_summary: str,
) -> AgentResult:
    data = provider.structured_output(
        f"Implement this plan in the workspace.\nPlan: {plan.model_dump()}\n"
        f"Objective: {task.objective}\nIssue:\n{task.issue_body}\nContext:\n{context_summary[:8000]}\n"
        "If you can write files, return JSON {\"files\":[{\"path\":str,\"content\":str}]} "
        "or {\"patch\":str} using *** REPLACE path / old / *** WITH / new.",
        system="You are the Implementation Agent. Minimal patch. Honor allowed/forbidden files. Do not touch main.",
        schema_hint='{"files":[{"path":str,"content":str}],"summary":str}',
    )
    changed: list[str] = []
    if isinstance(data.get("files"), list):
        for item in data["files"]:
            path = item.get("path")
            content = item.get("content")
            if not path or content is None:
                continue
            if any(path == f or path.startswith("harness/state") for f in plan.forbidden_files):
                continue
            tools.write_file(path=path, content=content)
            changed.append(path)
    elif data.get("patch"):
        res = tools.apply_patch(diff=str(data["patch"]))
        changed.extend(res.get("files") or [])
    store.write_json("coder.json", {"changed": changed, "raw": data})
    return AgentResult(agent="coder", success=bool(changed) or bool(data), summary=str(data.get("summary") or changed), data={"changed": changed})


def run_reviewer(task: TaskState, provider: ModelProvider, diff: str, store: HarnessStore) -> list[Finding]:
    data = provider.structured_output(
        f"Task: {task.objective}\nAcceptance: {[c.model_dump() for c in task.acceptance_criteria]}\nDiff:\n{diff[:12000]}",
        system="You are an adversarial reviewer. Try to prove the change is wrong. Return findings with evidence.",
        schema_hint='{"findings":[{"category":str,"severity":str,"confidence":float,"description":str,"evidence":[str],"file":str}]}',
    )
    findings: list[Finding] = []
    for i, f in enumerate(data.get("findings") or [], 1):
        if not isinstance(f, dict):
            continue
        if not f.get("evidence") and float(f.get("confidence") or 0) >= 0.8:
            continue  # no evidence, no high-confidence finding
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
                finding_id=f"F-{task.task_id}-{i:03d}",
                category=cat,
                severity=sev,
                confidence=float(f.get("confidence") or 0.5),
                file=f.get("file"),
                description=str(f.get("description") or ""),
                evidence=list(f.get("evidence") or []),
                agent="reviewer",
                iteration=task.iteration,
            )
        )
    store.write_json("review.json", {"findings": [x.model_dump() for x in findings]})
    return findings


def run_security(task: TaskState, diff: str, store: HarnessStore) -> list[Finding]:
    findings: list[Finding] = []
    needles = {
        "password": "Possible secret or credential handling",
        "api_key": "Possible secret",
        "eval(": "Dangerous eval",
        "exec(": "Dangerous exec",
        "pickle.loads": "Unsafe deserialization",
        "shell=True": "Command injection risk",
        "innerHTML": "XSS risk",
    }
    i = 1
    for needle, desc in needles.items():
        if needle in diff:
            findings.append(
                Finding(
                    finding_id=f"S-{task.task_id}-{i:03d}",
                    category=FindingCategory.SECURITY,
                    severity=Severity.HIGH,
                    confidence=0.7,
                    description=desc,
                    evidence=[f"diff contains `{needle}`"],
                    evidence_level=__import__("lcc.schemas", fromlist=["EvidenceLevel"]).EvidenceLevel.DATAFLOW,
                    agent="security",
                    iteration=task.iteration,
                )
            )
            i += 1
    store.write_json("security.json", {"findings": [x.model_dump() for x in findings]})
    return findings


def run_judge(
    task: TaskState,
    verification_ok: bool,
    findings: list[Finding],
    store: HarnessStore,
) -> JudgeResult:
    blocking = [
        f
        for f in findings
        if f.severity in {Severity.BLOCKER, Severity.CRITICAL, Severity.HIGH} and f.confidence >= 0.7 and f.evidence
    ]
    nonblocking = [f for f in findings if f not in blocking]
    if verification_ok and not blocking:
        decision = JudgeDecision.PASS
        conf = 0.9
    elif not verification_ok:
        decision = JudgeDecision.FAIL
        conf = 0.85
    elif blocking:
        decision = JudgeDecision.ITERATE
        conf = 0.7
    else:
        decision = JudgeDecision.PASS
        conf = 0.75
    result = JudgeResult(
        decision=decision,
        confidence=conf,
        blocking_findings=blocking,
        non_blocking_findings=nonblocking,
        evidence=[f"verification_ok={verification_ok}", f"blocking={len(blocking)}"],
        remaining_risk=[b.description for b in blocking],
    )
    store.write_json("judge.json", result)
    return result


def run_recovery(task: TaskState, failure: dict[str, Any], provider: ModelProvider, store: HarnessStore) -> dict[str, Any]:
    stdout = str(failure.get("stdout") or "")
    stderr = str(failure.get("stderr") or "")
    blob = (stdout + "\n" + stderr).lower()
    cls = FailureClass.UNKNOWN
    action = RecoveryAction.PATCH
    if "modulenotfounderror" in blob or "importerror" in blob:
        cls = FailureClass.DEPENDENCY_BUG
        action = RecoveryAction.RESEARCH
    elif "permission" in blob or "eacces" in blob:
        cls = FailureClass.ENVIRONMENT_BUG
        action = RecoveryAction.ESCALATE
    elif "assertionerror" in blob or "failed" in blob:
        cls = FailureClass.CODE_BUG
        action = RecoveryAction.PATCH
    elif "not found" in blob:
        cls = FailureClass.MISSING_CONTEXT
        action = RecoveryAction.RESEARCH
    data = provider.structured_output(
        f"Classify this failure.\n{failure}\nHeuristic class={cls} action={action}",
        system="You are the Recovery Agent. Classify failure and choose patch, research, re-plan, rollback, retry, or escalate.",
        schema_hint='{"class":str,"action":str,"notes":str}',
    )
    try:
        cls = FailureClass(str(data.get("class") or cls.value))
    except ValueError:
        pass
    try:
        action = RecoveryAction(str(data.get("action") or action.value))
    except ValueError:
        pass
    out = {"class": cls.value, "action": action.value, "notes": data.get("notes")}
    store.write_json(f"recovery_{task.iteration}.json", out)
    return out


def needs_security(task: TaskState) -> bool:
    blob = f"{task.objective} {task.issue_body} {task.risk}".lower()
    return any(k in blob for k in ("auth", "password", "token", "payment", "secret", "session", "permission", "xss", "inject"))
