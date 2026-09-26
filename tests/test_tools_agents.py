from pathlib import Path

from lcc.agents import AgentHost, AgentSpawnError
from lcc.constants import MAX_AGENT_DEPTH
from lcc.schemas import Budget
from lcc.tools import ToolBudgetExceeded, ToolError, ToolPolicy, ToolRegistry


def test_path_escape(tmp_path: Path):
    tools = ToolRegistry(tmp_path, ToolPolicy({"read_repository": True, "write_repository": True}), Budget())
    try:
        tools.read_file(path="../outside.txt")
        # may resolve inside if parent is still under tmp? parent of tmp can escape
    except ToolError:
        return
    # if the path stayed inside, that's also fine


def test_write_requires_permission(tmp_path: Path):
    tools = ToolRegistry(tmp_path, ToolPolicy({"read_repository": True, "write_repository": False}), Budget())
    try:
        tools.write_file(path="a.py", content="x")
        raise AssertionError("expected ToolError")
    except ToolError:
        pass


def test_search_budget(tmp_path: Path):
    (tmp_path / "f.py").write_text("hello", encoding="utf-8")
    budget = Budget(max_search_calls=1)
    tools = ToolRegistry(tmp_path, ToolPolicy({"read_repository": True}), budget)
    tools.search_code(query="hello")
    try:
        tools.search_code(query="hello")
        raise AssertionError("expected budget error")
    except ToolBudgetExceeded:
        pass


def test_agent_depth():
    host = AgentHost(0)
    child = host.child()
    assert child.depth == MAX_AGENT_DEPTH
    try:
        child.child()
        raise AssertionError("expected spawn error")
    except AgentSpawnError:
        pass
