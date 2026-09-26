"""Repo-only mode: given just a repository, find what is worth fixing without being told.

Three sources, cheapest and most trustworthy first:
1. the repository's own test suite: tests that fail today are real, reproducible problems;
2. static analysis (ruff) restricted to rules that flag actual defects, not style;
3. a token-bounded model audit of the most central source files, asked only for concrete defects with a
   triggering input (bugs, security holes, clear algorithmic waste).

Every candidate is then fixed through the normal pipeline, whose verifier demands proof (a test that fails
before the change and passes after it; for optimizations, the existing tests passing with no regressions).
A model "finding" that cannot be demonstrated is dropped there, so hallucinated bugs never reach a PR.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lcc.agents import as_list
from lcc.context_engine import _is_test, pagerank, scan_repo
from lcc.model import BaseProvider
from lcc.schemas import Budget
from lcc.tools import ToolPolicy, ToolRegistry

# ruff rules that point at real defects (wrong behavior, crashes, security), never at style.
DEFECT_RULES = {
    "F821": "undefined name (NameError at runtime)",
    "F632": "`is` comparison with a literal (always compares identity, not value)",
    "F601": "dictionary key repeated with different values",
    "F811": "definition shadowed by a later redefinition",
    "F701": "`break` outside a loop", "F702": "`continue` outside a loop",
    "F501": "invalid %-format string", "F502": "%-format expects a mapping", "F503": "%-format expects a sequence",
    "F507": "%-format argument count mismatch", "F522": ".format() unused named argument",
    "F523": ".format() unused positional argument", "F524": ".format() missing argument",
    "B006": "mutable default argument (shared between calls)",
    "B012": "return/break/continue inside finally swallows exceptions",
    "B015": "comparison result is discarded (probably meant an assert)",
    "B018": "useless expression (result discarded)",
    "B020": "loop variable overrides the iterable it iterates",
    "B023": "closure captures a loop variable (late binding)",
    "B025": "duplicate exception handler",
    "B033": "duplicate item in a set literal",
    "PLE0101": "return with a value in __init__",
    "PLE1142": "await outside an async function",
    "S307": "eval() on data (code injection)",
    "S506": "yaml.load without SafeLoader (code execution)",
    "S602": "subprocess with shell=True (command injection)",
    "S608": "SQL built with string formatting (SQL injection)",
}
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs", ".java", ".rb", ".php", ".cs", ".kt", ".c", ".cc", ".cpp", ".h"}
SKIP_PARTS = {"docs", "doc", "examples", "example", "vendor", "third_party", "migrations", "dist", "build", "site-packages"}

AUDIT_SYSTEM = (
    "You are a senior code auditor. Read the numbered source files and report ONLY concrete defects you can "
    "demonstrate: wrong results, crashes on valid input, unhandled edge cases the code clearly intends to handle, "
    "security holes, resource leaks, or algorithmic waste with a clear big-O cost on realistic inputs. For each "
    "give the exact file and line, a concrete input or call that triggers it, and the expected vs actual behavior. "
    "No style, naming, typing, docs, or speculative issues. At most 5 findings, most severe first. An empty list "
    "is a good answer when the code is correct."
)
AUDIT_SCHEMA = (
    '{"findings":[{"file":str,"line":int,"kind":"bug|security|performance","title":str,"why":str,'
    '"trigger":str,"expected":str,"actual":str,"confidence":float}]}'
)


@dataclass
class Candidate:
    source: str  # failing_test | static | audit
    kind: str  # bug | security | performance
    title: str
    body: str
    files: list[str] = field(default_factory=list)
    line: int = 0
    score: float = 0.0

    def issue_text(self) -> str:
        return f"{self.title}\n\n{self.body}\n\n(Found automatically by the LCC harness: {self.source}.)"


def discover(repo: Path, provider: BaseProvider | None, *, max_candidates: int = 3, audit_calls: int = 2,
             audit_chars: int = 18_000, budget: Budget | None = None, log=print) -> list[Candidate]:
    budget = budget or Budget()
    found: list[Candidate] = []
    log("scanning: running the test suite")
    found += from_failing_tests(repo, budget)
    log(f"scanning: static analysis ({len(found)} candidate(s) so far)")
    found += from_static_analysis(repo)
    if provider is not None and audit_calls > 0:
        log("scanning: model audit of the central source files")
        found += from_model_audit(repo, provider, calls=audit_calls, chars=audit_chars)
    return rank(found, max_candidates)


# ------------------------------------------------------------------ 1. failing tests
def from_failing_tests(repo: Path, budget: Budget) -> list[Candidate]:
    tools = ToolRegistry(repo, ToolPolicy({"read_repository": True, "run_tests": True}), budget)
    try:
        res = tools.run_test()
    except Exception:  # noqa: BLE001 - no runnable suite is not an error here
        return []
    if res.get("ok") or not res.get("tests_run"):
        return []
    by_file: dict[str, list[str]] = {}
    for tid in res.get("failed_ids") or []:
        by_file.setdefault(tid.split("::")[0] if "::" in tid else tid, []).append(tid)
    output = f"{res.get('stdout') or ''}\n{res.get('stderr') or ''}"
    if not by_file:
        by_file = {"test suite": [f"{res.get('tests_failed')} failing test(s)"]}
    out = []
    for where, ids in list(by_file.items())[:3]:
        out.append(Candidate(
            source="failing_test", kind="bug", title=f"Failing tests in {where}",
            body=("These tests fail on the current default branch:\n" + "\n".join(f"- {i}" for i in ids[:10]) +
                  f"\n\nTest output (tail):\n```\n{output[-2500:]}\n```\nFind the root cause in the code under test "
                  "(or in the test, if the test itself is wrong) and fix it. Do not delete or skip tests."),
            files=[where] if where.endswith((".py", ".js", ".ts", ".go", ".rs")) else [], score=0.95))
    return out


# ------------------------------------------------------------------ 2. static analysis
def from_static_analysis(repo: Path) -> list[Candidate]:
    if not any(repo.glob("**/*.py")):
        return []
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--isolated", "--output-format", "json",
         "--select", ",".join(DEFECT_RULES), "--exit-zero", "."],
        cwd=repo, text=True, capture_output=True, timeout=300,
    )
    try:
        rows = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        rel = Path(r.get("filename", "")).resolve()
        try:
            rel_s = rel.relative_to(repo.resolve()).as_posix()
        except ValueError:
            continue
        if _is_test(rel_s) or any(p in SKIP_PARTS for p in Path(rel_s).parts) or rel_s.startswith(("harness/", ".")):
            continue
        groups.setdefault((r.get("code") or "", rel_s), []).append(r)
    out = []
    for (code, rel), hits in groups.items():
        lines = [h.get("location", {}).get("row", 0) for h in hits]
        where = ", ".join(f"line {n}" for n in lines[:5])
        security = code.startswith("S")
        out.append(Candidate(
            source="static", kind="security" if security else "bug",
            title=f"{DEFECT_RULES.get(code, code)} in {rel}",
            body=(f"Static analysis (ruff {code}) reports a defect in `{rel}` at {where}:\n" +
                  "\n".join(f"- {rel}:{h.get('location', {}).get('row')}: {h.get('message')}" for h in hits[:5]) +
                  "\n\nConfirm the defect with a test that fails because of it, then fix it. If a hit is intentional "
                  "and harmless, leave it and fix only the real ones."),
            files=[rel], line=lines[0] if lines else 0, score=0.8 if security or code in {"F821", "B006", "B023", "F632"} else 0.6))
    return out


# ------------------------------------------------------------------ 3. model audit
def audit_files(repo: Path, chars: int, calls: int) -> list[list[tuple[str, str]]]:
    """The most central source files (import-graph PageRank), packed into `calls` chunks of <= `chars`."""
    index = scan_repo(repo)
    ranks = pagerank(index.graph, index.files)
    sources = [f for f in index.files if Path(f).suffix in SOURCE_SUFFIXES and not _is_test(f)
               and not any(p in SKIP_PARTS for p in Path(f).parts) and not Path(f).name.startswith("__init__")]
    sources.sort(key=lambda f: ranks.get(f, 0), reverse=True)
    chunks: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    used = 0
    for rel in sources:
        try:
            text = (repo / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not text.strip() or len(text) > chars:
            continue
        numbered = "\n".join(f"{i:>4}| {line}" for i, line in enumerate(text.splitlines(), 1))
        if used + len(numbered) > chars and current:
            chunks.append(current)
            if len(chunks) >= calls:
                return chunks
            current, used = [], 0
        current.append((rel, numbered))
        used += len(numbered)
    if current and len(chunks) < calls:
        chunks.append(current)
    return chunks


def from_model_audit(repo: Path, provider: BaseProvider, *, calls: int = 2, chars: int = 18_000) -> list[Candidate]:
    out = []
    for chunk in audit_files(repo, chars, calls):
        prompt = "\n\n".join(f"### {rel}\n{body}" for rel, body in chunk)
        data = provider.structured_output(prompt, system=AUDIT_SYSTEM, schema_hint=AUDIT_SCHEMA, agent="auditor")
        known = {rel for rel, _ in chunk}
        for f in as_list(data.get("findings")):
            if not isinstance(f, dict):
                continue
            rel = str(f.get("file") or "").lstrip("./")
            conf = _float(f.get("confidence"))
            kind = str(f.get("kind") or "bug").lower()
            kind = kind if kind in {"bug", "security", "performance"} else "bug"
            if rel not in known or not str(f.get("trigger") or "").strip() or conf < (0.7 if kind == "performance" else 0.6):
                continue
            line = int(_float(f.get("line")))
            title = re.sub(r"\s+", " ", str(f.get("title") or "defect")).strip()[:100]
            body = (f"{f.get('why') or ''}\n\nLocation: `{rel}` line {line}\n"
                    f"How to trigger: {f.get('trigger')}\nExpected: {f.get('expected') or '?'}\nActual: {f.get('actual') or '?'}")
            if kind == "performance":
                body += ("\n\nThis is an optimization: keep every result identical and make the existing tests keep "
                         "passing. If the defect is not real, change nothing and call finish.")
            else:
                body += "\n\nFirst write a test that reproduces this. If it does not fail, the finding is wrong: change nothing."
            out.append(Candidate(source="audit", kind=kind, title=f"{title} ({rel})", body=body, files=[rel],
                                 line=line, score=0.5 + 0.4 * conf - (0.1 if kind == "performance" else 0)))
    return out


def rank(found: list[Candidate], limit: int) -> list[Candidate]:
    out: list[Candidate] = []
    for c in sorted(found, key=lambda c: c.score, reverse=True):
        dup = any(c.files and o.files and c.files[0] == o.files[0] and abs(c.line - o.line) <= 3 for o in out)
        if not dup:
            out.append(c)
        if len(out) >= limit:
            break
    return out


def _float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
