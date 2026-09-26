from __future__ import annotations

import re
from pathlib import Path

from lcc.schemas import Rule, Severity

RULE_FILES = [
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "CONTRIBUTING.md",
    ".github/copilot-instructions.md",
]

RULE_GLOBS = [
    ".cursor/rules/**/*.mdc",
    ".cursor/rules/**/*.md",
    ".github/instructions/**/*.md",
    ".github/instructions/**/*.instructions.md",
]


def discover_rules(root: Path) -> list[Rule]:
    root = Path(root)
    found: list[Rule] = []
    n = 1
    for rel in RULE_FILES:
        path = root / rel
        if path.is_file():
            found.extend(_parse_markdown_rules(path, rel, n))
            n = len(found) + 1
    for pattern in RULE_GLOBS:
        for path in root.glob(pattern):
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                found.extend(_parse_markdown_rules(path, rel, len(found) + 1))
    found.extend(_config_rules(root, len(found) + 1))
    return _dedupe(found)


def _parse_markdown_rules(path: Path, source: str, start: int) -> list[Rule]:
    text = path.read_text(encoding="utf-8", errors="replace")
    scope = _front_matter_globs(text) or _scope_from_source(source)
    chunks = re.split(r"\n(?=#{1,3}\s)", text)
    rules: list[Rule] = []
    idx = start
    if len(chunks) <= 1:
        body = text.strip()
        if body:
            rules.append(
                Rule(
                    id=f"RULE-{idx:03d}",
                    scope=scope,
                    instruction=body[:4000],
                    source=source,
                    verification="human or verifier checks instruction",
                    severity=_severity_of(body),
                )
            )
        return rules
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk or chunk.startswith("---"):
            continue
        rules.append(
            Rule(
                id=f"RULE-{idx:03d}",
                scope=scope,
                instruction=chunk[:4000],
                source=source,
                verification="mapped to tests/lint when possible",
                severity=_severity_of(chunk),
            )
        )
        idx += 1
    return rules


def _front_matter_globs(text: str) -> str | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    fm = text[3:end]
    m = re.search(r"globs:\s*(.+)", fm)
    if m:
        return m.group(1).strip().strip("\"'")
    return None


def _scope_from_source(source: str) -> str:
    if "instructions/" in source:
        return "**"
    if source.endswith(".mdc"):
        return "**"
    return "**"


def _severity_of(text: str) -> Severity:
    t = text.lower()
    if any(w in t for w in ("must not", "never", "critical", "secret", "security")):
        return Severity.CRITICAL
    if "must " in t or "do not" in t:
        return Severity.HIGH
    if "should" in t:
        return Severity.MEDIUM
    return Severity.LOW


def _config_rules(root: Path, start: int) -> list[Rule]:
    rules: list[Rule] = []
    idx = start
    mapping = {
        "pyproject.toml": "Use project Python tooling from pyproject.toml.",
        "package.json": "Honor package.json scripts for test/lint/build.",
        ".eslintrc.json": "Run eslint as configured.",
        "ruff.toml": "Run ruff as configured.",
        ".pre-commit-config.yaml": "Respect pre-commit hooks.",
    }
    for name, instruction in mapping.items():
        if (root / name).exists():
            rules.append(
                Rule(
                    id=f"RULE-{idx:03d}",
                    scope="**",
                    instruction=instruction,
                    source=name,
                    verification=f"config file {name} exists",
                    severity=Severity.MEDIUM,
                )
            )
            idx += 1
    return rules


def _dedupe(rules: list[Rule]) -> list[Rule]:
    seen: dict[str, Rule] = {}
    out: list[Rule] = []
    for r in rules:
        key = re.sub(r"\s+", " ", r.instruction[:240].lower())
        if key in seen:
            seen[key].conflicts_with.append(r.id)
            continue
        seen[key] = r
        out.append(r)
    return out


def rules_for_paths(rules: list[Rule], paths: list[str]) -> list[Rule]:
    selected: list[Rule] = []
    for rule in rules:
        if rule.scope in {"**", "*"}:
            selected.append(rule)
            continue
        glob = rule.scope.replace("**/", "").replace("**", "")
        if any(_match(p, rule.scope) or glob in p for p in paths):
            selected.append(rule)
    return selected or rules


def _match(path: str, pattern: str) -> bool:
    if pattern in {"**", "*"}:
        return True
    regex = "^" + pattern.replace(".", r"\.").replace("**", ".*").replace("*", "[^/]*") + "$"
    return re.search(regex, path) is not None
