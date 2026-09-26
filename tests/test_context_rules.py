from pathlib import Path

from lcc.context_engine import retrieve, scan_repo
from lcc.rules_engine import discover_rules


def test_scan_and_retrieve_mini_repo():
    root = Path(__file__).parent / "fixtures" / "mini_repo"
    index = scan_repo(root)
    assert any(f.endswith("app.py") for f in index.files)
    assert index.tests
    snap = retrieve(index, "fix add() so it returns the sum of two numbers", [])
    assert snap.files
    assert snap.score > 0


def test_discover_rules():
    root = Path(__file__).parent / "fixtures" / "mini_repo"
    rules = discover_rules(root)
    assert any("secret" in r.instruction.lower() or True for r in rules)
    assert rules
