from __future__ import annotations

from lcc.schemas import Finding, JudgeResult, TaskState, VerificationResult

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
    context_score: float,
    verification: VerificationResult | None,
    judge: JudgeResult | None,
    findings: list[Finding],
) -> dict[str, float]:
    intake = min(10.0, (getattr(task, "global_score", 0) and 8) or 8)
    # use presence of structured fields
    issue = 10 if task.acceptance_criteria else 6
    ctx_rel = min(10.0, context_score / 10)
    ctx_comp = min(10.0, context_score / 10)
    plan = 8 if task.plan_version else 3
    impl = 16 if verification and verification.passed else (6 if task.affected_files else 4)
    test = 15 if verification and verification.passed else (5 if verification else 0)
    rules = 8 if task.applicable_rules else 5
    if any(f.category.value == "RULES" and f.severity.value in {"BLOCKER", "CRITICAL", "HIGH"} for f in findings):
        rules = 3
    regression = 5 if not any("regression" in (task.last_failure or "").lower() for _ in [0]) else 2
    eff = 5
    if task.budget.tokens:
        used = task.budget.tokens_used / task.budget.tokens
        if used > 0.9:
            eff = 2
        elif used > 0.6:
            eff = 3
    recovery = 4 if task.iteration <= 2 else max(1, 5 - task.iteration)
    parts = {
        "issue_understanding": issue,
        "context_relevance": ctx_rel,
        "context_completeness": ctx_comp,
        "plan_correctness": plan,
        "implementation_correctness": impl,
        "test_evidence": test,
        "rule_compliance": rules,
        "regression_safety": regression,
        "efficiency": eff,
        "recovery_quality": recovery,
    }
    parts["total"] = sum(parts.values())
    return parts


def threshold_decision(total: float) -> str:
    if total >= 90:
        return "verified"
    if total >= 80:
        return "acceptable_with_review"
    if total >= 70:
        return "needs_iteration"
    return "replan"
