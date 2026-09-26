from lcc.schemas import TaskStatus, TaskState
from lcc.state_machine import IllegalTransition, transition


def test_happy_path_transitions():
    task = TaskState(task_id="T", repository="r", workspace=".", objective="x")
    assert task.status == TaskStatus.RECEIVED
    transition(task, TaskStatus.ANALYZING)
    transition(task, TaskStatus.CONTEXT_BUILDING)
    transition(task, TaskStatus.RULE_RESOLUTION)
    transition(task, TaskStatus.PLANNING)


def test_illegal_transition():
    task = TaskState(task_id="T", repository="r", workspace=".", objective="x")
    try:
        transition(task, TaskStatus.IMPLEMENTING)
        raise AssertionError("expected IllegalTransition")
    except IllegalTransition:
        pass
