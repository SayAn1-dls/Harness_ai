import shutil
from pathlib import Path

from lcc.model import MockProvider
from lcc.orchestrator import Orchestrator, create_task
from lcc.schemas import TaskStatus
from lcc.store import HarnessStore


def test_e2e_mini_repo(tmp_path: Path):
    src = Path(__file__).parent / "fixtures" / "mini_repo"
    dest = tmp_path / "repo"
    shutil.copytree(src, dest)
    store = HarnessStore(dest)
    task = create_task(
        store,
        "GH-1",
        "Fix add() so it returns the sum of two numbers. add(2,3) must equal 5.",
        dest,
        issue_body="The add function currently subtracts. Tests in tests/test_app.py fail.",
    )
    orch = Orchestrator(store, MockProvider())
    result = orch.run(task)
    assert (dest / "app.py").read_text(encoding="utf-8").find("return a + b") >= 0
    assert result.status in {TaskStatus.HUMAN_REVIEW, TaskStatus.VERIFIED}
    assert store.events_path.exists()
    assert (store.artifacts / "HANDOFF.md").exists()
    assert result.context_snapshot
    dumped = store.load_task()
    assert dumped is not None
    assert dumped.task_id == "GH-1"
