from lcc.lanes import complexity_score, select_lane
from lcc.schemas import Lane, TaskState


def test_lane_selection():
    task = TaskState(task_id="T", repository="r", workspace=".", objective="typo in readme", risk="low")
    score = complexity_score(task, file_count=1, languages=1, rule_count=1, dep_depth=0)
    assert select_lane(score) == Lane.A
    task.risk = "critical"
    task.ambiguities = ["a", "b", "c", "d"]
    score = complexity_score(task, file_count=5, languages=3, rule_count=20, dep_depth=3)
    assert select_lane(score) == Lane.C
