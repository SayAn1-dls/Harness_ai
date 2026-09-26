from __future__ import annotations

from lcc.schemas import TaskStatus, TaskState

LEGAL_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.RECEIVED: {TaskStatus.ANALYZING, TaskStatus.STOPPED},
    TaskStatus.ANALYZING: {TaskStatus.CONTEXT_BUILDING, TaskStatus.ESCALATED, TaskStatus.STOPPED},
    TaskStatus.CONTEXT_BUILDING: {TaskStatus.RULE_RESOLUTION, TaskStatus.STOPPED},
    TaskStatus.RULE_RESOLUTION: {TaskStatus.IMPACT_ANALYSIS, TaskStatus.PLANNING, TaskStatus.STOPPED},
    TaskStatus.IMPACT_ANALYSIS: {TaskStatus.PLANNING, TaskStatus.STOPPED},
    TaskStatus.PLANNING: {TaskStatus.PLAN_VALIDATION, TaskStatus.STOPPED},
    TaskStatus.PLAN_VALIDATION: {TaskStatus.READY_TO_EXECUTE, TaskStatus.PLANNING, TaskStatus.STOPPED},
    TaskStatus.READY_TO_EXECUTE: {TaskStatus.IMPLEMENTING, TaskStatus.STOPPED},
    TaskStatus.IMPLEMENTING: {TaskStatus.TESTING, TaskStatus.FAILED, TaskStatus.STOPPED},
    TaskStatus.TESTING: {TaskStatus.REVIEWING, TaskStatus.FAILED, TaskStatus.JUDGING, TaskStatus.STOPPED},
    TaskStatus.REVIEWING: {TaskStatus.JUDGING, TaskStatus.FAILED, TaskStatus.STOPPED},
    TaskStatus.JUDGING: {
        TaskStatus.VERIFIED,
        TaskStatus.FAILED,
        TaskStatus.HUMAN_REVIEW,
        TaskStatus.STOPPED,
    },
    TaskStatus.FAILED: {TaskStatus.DIAGNOSING, TaskStatus.STOPPED, TaskStatus.ESCALATED},
    TaskStatus.DIAGNOSING: {TaskStatus.CONTEXT_UPDATE, TaskStatus.REPLANNING, TaskStatus.ESCALATED, TaskStatus.STOPPED},
    TaskStatus.CONTEXT_UPDATE: {TaskStatus.REPLANNING, TaskStatus.STOPPED},
    TaskStatus.REPLANNING: {TaskStatus.IMPLEMENTING, TaskStatus.STOPPED},
    TaskStatus.VERIFIED: {TaskStatus.HUMAN_REVIEW, TaskStatus.PR_READY, TaskStatus.STOPPED},
    TaskStatus.HUMAN_REVIEW: {TaskStatus.PR_READY, TaskStatus.STOPPED, TaskStatus.REPLANNING},
    TaskStatus.PR_READY: {TaskStatus.STOPPED},
    TaskStatus.STOPPED: set(),
    TaskStatus.ESCALATED: {TaskStatus.STOPPED, TaskStatus.HUMAN_REVIEW},
}


class IllegalTransition(ValueError):
    pass


def transition(task: TaskState, dest: TaskStatus) -> TaskState:
    allowed = LEGAL_TRANSITIONS.get(task.status, set())
    if dest != task.status and dest not in allowed:
        raise IllegalTransition(f"{task.status.value} -> {dest.value} is not legal")
    task.status = dest
    task.touch()
    return task
