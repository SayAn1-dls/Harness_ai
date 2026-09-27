"""Proof that travels with a fix, and proof that the fix's test is worth something.

1. Proof-carrying fixes: every verified fix records its base commit, head commit and the tests that must flip.
   `lcc verify` replays it anywhere (tests fail on the base sources, pass on the fix), and `manual_steps` renders the
   same check as plain git + test commands for a maintainer who does not want to install anything.
2. Test strength (mutation check): the lines the fix added are broken on purpose, one small change at a time
   (flip a comparison, an off-by-one, and/or, True/False, None checks...). A test worth keeping fails on most of
   them. A test that survives every deliberate break is decoration.

No model call happens here: proof is test runs only.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from lcc.context_engine import _is_test
from lcc.schemas import Budget
from lcc.tools import ToolPolicy, ToolRegistry


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, timeout=300)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {proc.stderr.strip()[-300:]}")
    return proc.stdout


# ------------------------------------------------------------------ 1. proof-carrying fixes
def make_proof(base: str, head: str, targets: list[str], kind: str, level: int,
               mutation: dict | None = None, speed: dict | None = None) -> dict[str, Any]:
    """kind: new_tests (new/changed tests fail on base), baseline_fixed (tests already failing now pass),
    behavior_preserved (optimization: existing tests keep passing)."""
    files = sorted({t.split("::")[0] for t in targets})
    return {"version": 1, "base": base, "head": head, "kind": kind, "level": level,
            "targets": targets, "test_files": files, "mutation": mutation or {}, "speed": speed or {}}


def manual_steps(proof: dict[str, Any], head_ref: str = "") -> str:
    """The same check as copy-paste commands: no harness needed."""
    head = head_ref or proof["head"][:12]
    files = " ".join(proof["test_files"])
    if proof["kind"] == "behavior_preserved":
        sp = proof.get("speed") or {}
        timing = f"\n# measured by the harness: {sp['message']}" if sp.get("message") else ""
        return f"git checkout {head}\npython -m pytest   # all pass: behavior is unchanged{timing}"
    return (f"git checkout {proof['base'][:12]}\n"
            f"git checkout {head} -- {files}        # the fix's tests, on the ORIGINAL code\n"
            f"python -m pytest {' '.join(proof['targets'])}   # expected: FAIL (the bug is real)\n"
            f"git checkout {head} -- .\n"
            f"python -m pytest {' '.join(proof['targets'])}   # expected: PASS (the fix works)")


def verify(repo: Path, base: str, head: str, targets: list[str] | None = None) -> dict[str, Any]:
    """Replay the proof in a throwaway worktree: with the base versions of the changed source files the targets
    must fail; with the head versions they must pass. Nothing in `repo` is modified."""
    repo = Path(repo).resolve()
    changed = [f for f in _git(repo, "diff", "--name-only", base, head).split() if f]
    targets = targets or [f for f in changed if _is_test(f)]
    if not targets:
        return {"valid": False, "reason": "no test targets: nothing proves this change"}
    sources = [f for f in changed if not _is_test(f)]
    work = Path(tempfile.mkdtemp(prefix="lcc-verify-")) / "wt"
    _git(repo, "worktree", "add", "--quiet", "--detach", str(work), head)
    try:
        for rel in sources:  # the ORIGINAL code with the fix's tests
            if subprocess.run(["git", "cat-file", "-e", f"{base}:{rel}"], cwd=work, capture_output=True).returncode == 0:
                _git(work, "checkout", base, "--", rel)
            elif (work / rel).exists():  # a file the fix created
                (work / rel).unlink()
        before = _run_targets(work, targets)
        _git(work, "checkout", "--force", head, "--", ".")  # the fix itself
        after = _run_targets(work, targets)
    finally:
        _git(repo, "worktree", "remove", "--force", str(work), check=False)
        shutil.rmtree(work.parent, ignore_errors=True)
    fails_on_base = not before["ok"]
    return {"valid": fails_on_base and after["ok"], "fails_on_base": fails_on_base, "passes_on_head": after["ok"],
            "base_output": before["tail"], "head_output": after["tail"], "targets": targets, "sources": sources}


def _run_targets(work: Path, targets: list[str]) -> dict[str, Any]:
    tools = ToolRegistry(work, ToolPolicy({"read_repository": True, "run_tests": True}), Budget())
    results = [tools.run_test(target=t) for t in targets]
    tail = "\n".join(f"{r['stdout']}\n{r['stderr']}".strip()[-600:] for r in results)
    return {"ok": all(r["ok"] for r in results), "tail": tail}


# ------------------------------------------------------------------ 2. test strength (mutation check)
# (pattern, replacement, name): small, realistic breakages of the lines a fix added.
OPERATORS: list[tuple[str, str, str]] = [
    (r" is not None\b", " is None", "is not None → is None"),
    (r" is None\b", " is not None", "is None → is not None"),
    (r" == ", " != ", "== → !="),
    (r" != ", " == ", "!= → =="),
    (r" <= ", " < ", "<= → <"),
    (r" >= ", " > ", ">= → >"),
    (r" < ", " <= ", "< → <="),
    (r" > ", " >= ", "> → >="),
    (r" and ", " or ", "and → or"),
    (r" or ", " and ", "or → and"),
    (r"\bnot ", "", "drop not"),
    (r"\bTrue\b", "False", "True → False"),
    (r"\bFalse\b", "True", "False → True"),
    (r" \+ 1\b", " - 1", "+1 → -1"),
    (r" - 1\b", " + 1", "-1 → +1"),
    (r"(?<![\w.])(\d+)(?![\w.])", "__NUM__", "n → n+1"),
    (r"^(\s*)return (?!None\b).+$", r"\1return None", "return → return None"),
    (r"^(\s*)raise \w.*$", r"\1pass", "raise → pass"),
]
SKIP_LINE = re.compile(r"^\s*(#|def |class |import |from |@|\"\"\"|'''|$)")


def added_lines(root: Path, files: list[str], against: str = "HEAD") -> list[tuple[str, int, str]]:
    """(file, 1-based line, text) for every line the change added to these files."""
    out: list[tuple[str, int, str]] = []
    diff = _git(root, "diff", "-U0", "--no-color", against, "--", *files, check=False)
    current, line_no = "", 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@"):
            m = re.search(r"\+(\d+)", line)
            line_no = int(m.group(1)) if m else 0
        elif line.startswith("+") and current:
            out.append((current, line_no, line[1:]))
            line_no += 1
    return out


STRING = re.compile(r"""(?:[rbuf]{0,2})("{3}|'{3}|"|')(?:\\.|(?!\1).)*\1|\#.*$""", re.I)


def _mask_strings(text: str) -> str:
    """Same length as `text`, with string literals and comments blanked: mutating a message text or a comment
    creates a break no test can see, which would make a good test look weak."""
    return STRING.sub(lambda m: m.group(0)[0] + " " * (len(m.group(0)) - 1), text)


def mutants(lines: list[tuple[str, int, str]], limit: int = 6) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for path, no, text in lines:
        if SKIP_LINE.match(text):
            continue
        masked = _mask_strings(text)
        for pattern, repl, name in OPERATORS:
            m = re.search(pattern, masked, re.M)
            if not m:
                continue
            if repl == "__NUM__":
                mutated = text[:m.start()] + str(int(m.group(1)) + 1) + text[m.end():]
            else:  # substitute on the masked text's match span, in the original line
                mutated = text[:m.start()] + m.expand(repl) + text[m.end():] if "\\1" in repl else \
                    text[:m.start()] + re.sub(pattern, repl, text[m.start():m.end()], count=1) + text[m.end():]
            if mutated != text:
                found.append({"file": path, "line": no, "op": name, "original": text, "mutated": mutated})
                break  # one mutant per line: breadth over depth
        if len(found) >= limit:
            break
    return found


def mutation_check(root: Path, targets: list[str], sources: list[str], limit: int = 6) -> dict[str, Any]:
    """Break each added source line once and run the fix's tests. killed = the tests noticed."""
    root = Path(root)
    py_sources = [s for s in sources if s.endswith(".py") and (root / s).is_file()]
    candidates = mutants(added_lines(root, py_sources), limit=limit * 2)
    tools = ToolRegistry(root, ToolPolicy({"read_repository": True, "run_tests": True}), Budget())
    killed, survived = [], []
    for mu in candidates:
        if len(killed) + len(survived) >= limit:
            break
        path = root / mu["file"]
        original = path.read_text(encoding="utf-8")
        lines = original.splitlines(keepends=True)
        idx = mu["line"] - 1
        if idx >= len(lines) or lines[idx].rstrip("\r\n") != mu["original"]:
            continue
        ending = lines[idx][len(mu["original"]):]
        lines[idx] = mu["mutated"] + ending
        mutated = "".join(lines)
        try:
            ast.parse(mutated)
        except SyntaxError:
            continue
        try:
            path.write_text(mutated, encoding="utf-8")
            caught = any(not tools.run_test(target=t)["ok"] for t in targets)
        finally:
            path.write_text(original, encoding="utf-8")
        entry = {"file": mu["file"], "line": mu["line"], "op": mu["op"], "code": mu["mutated"].strip()[:120]}
        (killed if caught else survived).append(entry)
    total = len(killed) + len(survived)
    return {"total": total, "killed": len(killed), "score": round(len(killed) / total, 2) if total else None,
            "survived": survived, "killed_ops": [k["op"] for k in killed]}
