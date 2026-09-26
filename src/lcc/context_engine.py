from __future__ import annotations

import ast
import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from lcc.constants import CONTEXT_SCORE_GATE
from lcc.schemas import ContextPacket, ContextSnapshot, utcnow

SKIP_DIRS = {
    ".git",
    ".hg",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    "target",
    ".tox",
    ".mypy_cache",
    "harness",
    ".lcc",
}
PROTECTED_DOTDIRS = {".github", ".cursor"}

CODE_SUFFIXES = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".c",
    ".h",
    ".cpp",
    ".cc",
    ".cs",
    ".swift",
    ".md",
    ".yml",
    ".yaml",
    ".toml",
    ".json",
}


@dataclass
class Symbol:
    name: str
    kind: str
    path: str
    line: int


@dataclass
class RepoIndex:
    root: Path
    files: list[str] = field(default_factory=list)
    languages: dict[str, int] = field(default_factory=dict)
    symbols: list[Symbol] = field(default_factory=list)
    imports: dict[str, list[str]] = field(default_factory=dict)
    tests: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    graph: dict[str, set[str]] = field(default_factory=dict)
    reverse_graph: dict[str, set[str]] = field(default_factory=dict)


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _walk(root: Path):
    """Sorted walk that prunes skipped directories instead of descending into them (node_modules can hold
    hundreds of thousands of files)."""
    import os

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and (not d.startswith(".") or d in PROTECTED_DOTDIRS))
        for name in sorted(filenames):
            yield Path(dirpath) / name


def scan_repo(root: Path, max_files: int = 4000) -> RepoIndex:
    root = Path(root).resolve()
    index = RepoIndex(root=root)
    count = 0
    for path in _walk(root):
        if path.suffix.lower() not in CODE_SUFFIXES and path.name not in {
            "Makefile",
            "Dockerfile",
            "AGENTS.md",
            "CLAUDE.md",
            "CONTRIBUTING.md",
            "README.md",
        }:
            continue
        rel = _rel(root, path)
        if rel.startswith("harness/"):
            continue
        index.files.append(rel)
        ext = path.suffix.lower() or path.name
        index.languages[ext] = index.languages.get(ext, 0) + 1
        if _is_test(rel):
            index.tests.append(rel)
        if path.name in {"main.py", "index.ts", "index.js", "app.py", "server.py", "main.go", "main.rs"}:
            index.entry_points.append(rel)
        count += 1
        if count >= max_files:
            break
    index.files.sort()
    _extract_all(index)
    _build_graph(index)
    return index


TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "testing"}
TEST_NAME = re.compile(
    r"(^test_.*\.py$|_test\.(py|go|rs|rb)$|_spec\.rb$|\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$|(Test|Tests|IT)\.(java|kt|cs)$)"
)


def _is_test(rel: str) -> bool:
    """Test file by directory (tests/, __tests__/, spec/ ...) or by the naming conventions of each ecosystem."""
    parts = Path(rel).parts
    return any(p.lower() in TEST_DIRS for p in parts[:-1]) or bool(TEST_NAME.search(parts[-1] if parts else rel))


def _extract_all(index: RepoIndex) -> None:
    for rel in index.files:
        path = index.root / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.suffix == ".py":
            _py_symbols(index, rel, text)
        else:
            _regex_symbols(index, rel, text)


def _py_symbols(index: RepoIndex, rel: str, text: str) -> None:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        _regex_symbols(index, rel, text)
        return
    imps: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            index.symbols.append(Symbol(node.name, "function", rel, getattr(node, "lineno", 1)))
        elif isinstance(node, ast.ClassDef):
            index.symbols.append(Symbol(node.name, "class", rel, getattr(node, "lineno", 1)))
        elif isinstance(node, ast.Import):
            for a in node.names:
                imps.append(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imps.append(node.module.split(".")[0])
    index.imports[rel] = imps


def _regex_symbols(index: RepoIndex, rel: str, text: str) -> None:
    for m in re.finditer(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", text, re.M):
        index.symbols.append(Symbol(m.group(1), "function", rel, text[: m.start()].count("\n") + 1))
    for m in re.finditer(r"^\s*(?:export\s+)?class\s+(\w+)", text, re.M):
        index.symbols.append(Symbol(m.group(1), "class", rel, text[: m.start()].count("\n") + 1))
    for m in re.finditer(r"^\s*def\s+(\w+)\s*\(", text, re.M):
        index.symbols.append(Symbol(m.group(1), "function", rel, text[: m.start()].count("\n") + 1))
    imps = re.findall(r"^\s*import\s+['\"]([^'\"]+)['\"]", text, re.M)
    imps += re.findall(r"^\s*from\s+['\"]([^'\"]+)['\"]", text, re.M)
    imps += re.findall(r"^\s*import\s+([a-zA-Z0-9_\.]+)", text, re.M)
    index.imports[rel] = [i.split("/")[-1].split(".")[0] for i in imps]


def _module_key(rel: str) -> str:
    p = Path(rel)
    return p.stem if p.suffix else rel


def _build_graph(index: RepoIndex) -> None:
    modules = {_module_key(f): f for f in index.files}
    graph: dict[str, set[str]] = defaultdict(set)
    for src, imps in index.imports.items():
        for imp in imps:
            target = modules.get(imp)
            if target and target != src:
                graph[src].add(target)
    index.graph = dict(graph)
    rev: dict[str, set[str]] = defaultdict(set)
    for src, dests in graph.items():
        for d in dests:
            rev[d].add(src)
    index.reverse_graph = dict(rev)


def pagerank(graph: dict[str, set[str]], nodes: Iterable[str], damping: float = 0.85, iters: int = 20) -> dict[str, float]:
    nodes = list(set(nodes))
    if not nodes:
        return {}
    n = len(nodes)
    rank = {node: 1 / n for node in nodes}
    inbound: dict[str, list[str]] = defaultdict(list)
    for src, dests in graph.items():
        for d in dests:
            inbound[d].append(src)
    for _ in range(iters):
        nxt = {node: (1 - damping) / n for node in nodes}
        for node in nodes:
            dests = [d for d in graph.get(node, set()) if d in rank]
            if not dests:
                share = rank[node] / n
                for t in nodes:
                    nxt[t] += damping * share
            else:
                share = rank[node] / len(dests)
                for d in dests:
                    nxt[d] += damping * share
        rank = nxt
    return rank


def repo_map(index: RepoIndex, token_budget: int = 2000) -> str:
    ranks = pagerank(index.graph, index.files)
    ranked = sorted(index.files, key=lambda f: ranks.get(f, 0), reverse=True)
    lines = ["# Repository map", f"root: {index.root}", f"files: {len(index.files)}", ""]
    used = 0
    for f in ranked:
        lang = Path(f).suffix
        syms = [s.name for s in index.symbols if s.path == f][:8]
        line = f"- {f} ({lang}) rank={ranks.get(f, 0):.4f} symbols={', '.join(syms)}"
        cost = max(8, len(line) // 4)
        if used + cost > token_budget:
            break
        lines.append(line)
        used += cost
    lines.append("")
    lines.append("## Tests")
    for t in index.tests[:40]:
        lines.append(f"- {t}")
    lines.append("")
    lines.append("## Entry points")
    for e in index.entry_points:
        lines.append(f"- {e}")
    return "\n".join(lines)


def _tokenize(text: str) -> set[str]:
    return {t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)}


def retrieve(
    index: RepoIndex,
    query: str,
    rules: list[str],
    token_budget: int = 12000,
    extra_files: Iterable[str] = (),
) -> ContextSnapshot:
    """Exact symbols -> token overlap -> one-hop dependencies -> tests -> compress into packets."""
    q = _tokenize(query)
    texts: dict[str, str] = {}
    toks: dict[str, set[str]] = {}
    for rel in index.files:
        try:
            texts[rel] = (index.root / rel).read_text(encoding="utf-8", errors="replace")[:80_000]
        except OSError:
            continue
        toks[rel] = _tokenize(texts[rel])

    sym_hits = {s.name.lower() for s in index.symbols} & q
    scores: dict[str, float] = {}
    for rel, tokens in toks.items():
        overlap = len(q & tokens)
        name_bonus = 8 if any(t in rel.lower() for t in q) else 0
        defines = sum(15 for s in index.symbols if s.path == rel and s.name.lower() in sym_hits)
        test_bonus = 3 if rel in index.tests and overlap else 0
        doc_penalty = 0.5 if rel.endswith((".md", ".json", ".yml", ".yaml", ".toml")) else 1.0
        scores[rel] = (overlap + name_bonus + defines + test_bonus) * doc_penalty
    for extra in extra_files:
        if extra in scores:
            scores[extra] += 25
    # one hop: pull in direct dependencies and dependents of strong candidates
    top = sorted((f for f in scores if scores[f] > 0), key=lambda f: scores[f], reverse=True)[:5]
    for f in top:
        for dep in index.graph.get(f, set()) | index.reverse_graph.get(f, set()):
            if dep in scores:
                scores[dep] += max(2.0, scores[f] * 0.3)

    ranked = sorted((f for f in scores if scores[f] > 0), key=lambda f: scores[f], reverse=True)
    packets: list[ContextPacket] = []
    files: list[str] = []
    used = 0
    selected_syms: list[str] = []
    tests: list[str] = []
    for rel in ranked:
        excerpt = texts[rel][:6000]
        cost = max(32, len(excerpt) // 4)
        if used + cost > token_budget:
            excerpt = excerpt[: max(400, (token_budget - used) * 4)]
            cost = max(16, len(excerpt) // 4)
            if used + cost > token_budget:
                break
        packets.append(ContextPacket(path=rel, reason=f"score={scores[rel]:.1f}", excerpt=excerpt, token_estimate=cost))
        files.append(rel)
        used += cost
        selected_syms.extend(f"{s.kind}:{s.name}" for s in index.symbols if s.path == rel)
        if rel in index.tests:
            tests.append(rel)
        if len(files) >= 24:
            break

    snapshot_id = "ctx_" + hashlib.sha1(f"{query}|{','.join(files)}".encode()).hexdigest()[:10]
    metric = _score_snapshot(files, ranked, toks, tests, rules, index, used, token_budget, q, sym_hits)
    return ContextSnapshot(
        snapshot_id=snapshot_id,
        created_at=utcnow(),
        score=metric["total"],
        files=files,
        symbols=selected_syms[:80],
        tests=tests,
        rules=rules,
        packets=packets,
        scores=metric,
        notes=["retrieve: symbols + overlap + one-hop deps + tests"],
    )


def _score_snapshot(
    files: list[str],
    ranked: list[str],
    toks: dict[str, set[str]],
    tests: list[str],
    rules: list[str],
    index: RepoIndex,
    used: int,
    budget: int,
    q: set[str],
    sym_hits: set[str],
) -> dict[str, float]:
    """Blueprint weights: files 20, symbols 15, deps 15, requirements 15, rules 10, tests 10, history 5, tokens 10."""
    chosen = set(files)
    want = ranked[: min(5, len(ranked))]
    rel_files = 20 * (sum(1 for f in want if f in chosen) / len(want)) if want else 0.0
    found_syms = {s.name.lower() for s in index.symbols if s.path in chosen} & sym_hits
    rel_syms = 15 * (len(found_syms) / len(sym_hits)) if sym_hits else 10.0
    needed = covered = 0
    for f in files[:8]:
        deps = index.graph.get(f, set())
        needed += len(deps)
        covered += sum(1 for d in deps if d in chosen)
    dep_cov = 15 * covered / needed if needed else 15.0
    repo_vocab = set().union(*toks.values()) if toks else set()
    answerable = q & repo_vocab
    covered_q = answerable & set().union(*(toks[f] for f in files if f in toks)) if files else set()
    req = 15 * len(covered_q) / len(answerable) if answerable else 0.0
    rule_cov = 10.0
    test_cov = 10.0 if tests or not index.tests else 0.0
    hist = 3.0  # history engine not implemented yet
    efficiency = max(0.0, 10 - (used / max(1, budget)) * 4)
    parts = {
        "relevant_files": round(rel_files, 2),
        "relevant_symbols": round(rel_syms, 2),
        "dependency_coverage": round(dep_cov, 2),
        "requirement_coverage": round(req, 2),
        "rule_coverage": rule_cov,
        "test_coverage": test_cov,
        "historical_relevance": hist,
        "token_efficiency": round(efficiency, 2),
    }
    parts["total"] = round(sum(parts.values()), 2)
    return parts


def snapshot_meets_gate(snap: ContextSnapshot) -> bool:
    return snap.score >= CONTEXT_SCORE_GATE


def persist_index(index: RepoIndex, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "root": str(index.root),
        "files": index.files,
        "languages": index.languages,
        "symbols": [s.__dict__ for s in index.symbols],
        "imports": index.imports,
        "tests": index.tests,
        "entry_points": index.entry_points,
        "graph": {k: sorted(v) for k, v in index.graph.items()},
        "reverse_graph": {k: sorted(v) for k, v in index.reverse_graph.items()},
    }
    dest.write_text(json.dumps(payload, indent=2), encoding="utf-8")
