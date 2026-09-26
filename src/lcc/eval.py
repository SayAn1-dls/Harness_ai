from __future__ import annotations

from lcc.schemas import (
    ContextSnapshot,
    FindingCategory,
    Finding,
    ImplementationPlan,
    JudgeResult,
    Severity,
    TaskState,
    VerificationResult,
)
from lcc.tools import _match

WEIGHTS = {
    "issue_understanding": 10,
    "context_relevance": 10,
    "context_completeness": 10,
    "plan_correctness": 10,
    "implementation_correctness": 20,
    "test_evidence": 15,
    "rule_compliance": 10,
    "regression_safety": 5,
    "efficiency": 5,
    "recovery_quality": 5,
}


def global_task_score(
    task: TaskState,
    intake_score: float,
    snapshot: ContextSnapshot | None,
    plan: ImplementationPlan | None,
    verification: VerificationResult | None,
    judge: JudgeResult | None,
    findings: list[Finding],
    changed: list[str],
) -> dict[str, float]:
    """Blueprint section 42. Every dimension is derived from recorded evidence, not constants."""
    s = snapshot.scores if snapshot else {}
    ctx_rel = (s.get("relevant_files", 0) + s.get("relevant_symbols", 0)) / 35 * 10
    ctx_comp = (s.get("dependency_coverage", 0) + s.get("requirement_coverage", 0) + s.get("test_coverage", 0)) / 40 * 10

    plan_score = 0.0
    if plan and plan.steps:
        plan_score = 5.0
        if changed:
            in_plan = [f for f in changed if not plan.allowed_files or any(_match(f, p) for p in plan.allowed_files)]
            plan_score += 5 * len(in_plan) / len(changed)

    cmd = verification.commands[0] if verification and verification.commands else {}
    run = int(cmd.get("tests_run") or 0)
    failed = int(cmd.get("tests_failed") or 0)
    pass_frac = (run - failed) / run if run else 0.0
    passed = bool(verification and verification.passed)
    impl = 20.0 if passed else 20 * pass_frac * 0.6
    touched_tests = any("test" in f.lower() for f in changed)
    test_ev = (12.0 if passed else 0.0) + (3.0 if touched_tests else 0.0)

    rule_hits = [f for f in findings if f.category == FindingCategory.RULES and f.severity in {Severity.BLOCKER, Severity.CRITICAL, Severity.HIGH}]
    rules = max(0.0, 10 - 4 * len(rule_hits))
    regression = 5.0 if passed else 5 * pass_frac

    used = task.budget.tokens_used / task.budget.tokens if task.budget.tokens else 0
    eff = max(1.0, 5 * (1 - used))
    recovery = 5.0 if task.iteration <= 1 else (max(1.0, 5 - (task.iteration - 1)) if passed else 1.0)

    parts = {
        "issue_understanding": round(intake_score / 10, 2),
        "context_relevance": round(ctx_rel, 2),
        "context_completeness": round(ctx_comp, 2),
        "plan_correctness": round(plan_score, 2),
        "implementation_correctness": round(impl, 2),
        "test_evidence": test_ev,
        "rule_compliance": rules,
        "regression_safety": round(regression, 2),
        "efficiency": round(eff, 2),
        "recovery_quality": recovery,
    }
    parts["total"] = round(sum(parts.values()), 2)
    return parts


def threshold_decision(total: float) -> str:
    if total >= 90:
        return "verified"
    if total >= 80:
        return "acceptable_with_review"
    if total >= 70:
        return "needs_iteration"
    return "replan"
