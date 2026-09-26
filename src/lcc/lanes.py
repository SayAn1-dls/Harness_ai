from __future__ import annotations

from lcc.schemas import Lane, TaskState


def complexity_score(task: TaskState, file_count: int, languages: int, rule_count: int, dep_depth: int) -> float:
    risk = {"low": 1, "medium": 2, "high": 3, "critical": 4}.get(task.risk, 1)
    amb = min(4, len(task.ambiguities))
    return (
        min(5, file_count)
        + min(3, dep_depth)
        + risk
        + amb
        + min(3, languages)
        + min(2, rule_count / 5)
        + (2 if "test" in task.objective.lower() else 1)
    )


def select_lane(score: float) -> Lane:
    if score < 6:
        return Lane.A
    if score < 12:
        return Lane.B
    return Lane.C


def agents_for_lane(lane: Lane, security: bool) -> list[str]:
    if lane == Lane.A:
        seq = ["intake", "coder", "verifier"]
    elif lane == Lane.B:
        seq = ["intake", "mapper", "rules", "planner", "coder", "verifier", "reviewer"]
    else:
        seq = [
            "intake",
            "mapper",
            "rules",
            "impact",
            "planner",
            "coder",
            "verifier",
            "reviewer",
            "judge",
        ]
        if security:
            seq.insert(-2, "security")
        return seq
    if lane == Lane.C:
        return seq
    if security and "security" not in seq:
        seq.insert(-1, "security")
    if lane != Lane.A and "judge" not in seq:
        seq.append("judge")
    return seq
