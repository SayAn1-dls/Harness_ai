"""Task score for reports. Every part comes from recorded evidence of this run; there are no hand-set constants."""

from __future__ import annotations

from lcc.schemas import ImplementationPlan, TaskState, VerificationResult
from lcc.tools import _match


def global_task_score(
    task: TaskState,
    plan: ImplementationPlan | None,
    verification: VerificationResult | None,
    changed: list[str],
) -> dict[str, float]:
    """verified 40 (the verifier passed), proof 20 (5 per proof level: 5 = fail-to-pass shown), tests 20 (share of
    the suite passing), scope 10 (changed files inside the plan), efficiency 10 (share of the token budget left)."""
    cmd = verification.commands[0] if verification and verification.commands else {}
    run = int(cmd.get("tests_run") or 0)
    failed = int(cmd.get("tests_failed") or 0)
    passed = bool(verification and verification.passed)
    in_scope = 1.0
    if plan and plan.allowed_files and changed:
        in_scope = sum(1 for f in changed if any(_match(f, p) for p in plan.allowed_files)) / len(changed)
    used = task.budget.tokens_used / task.budget.tokens if task.budget.tokens else 0.0
    parts = {
        "verified": 40.0 if passed else 0.0,
        "proof": 4.0 * int(task.verification.get("proof_level") or 0),
        "tests_passing": round(20 * (run - failed) / run, 2) if run else 0.0,
        "in_scope": round(10 * in_scope, 2),
        "efficiency": round(10 * max(0.0, 1 - used), 2),
    }
    parts["total"] = round(sum(parts.values()), 2)
    return parts
