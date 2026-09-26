# Architecture

## Differentiator

Context continuity across agents and iterations: same task ID, snapshot, graph, rules, plan, evidence, and history.

## System

```text
USER / GITHUB
      → Task gateway
      → Task state machine
      → Context / Rules / History engines
      → Context snapshot
      → Orchestrator (lane, budget, spawn)
      → Specialized agents (activated, not always-on)
      → Model adapter (Grok / Claude / GPT / mock)
      → Tools + workspace (never main)
      → Verify (tests, lint, types)
      → Review / Security / Judge
      → Recovery or Human approval → PR
```

## Failure modes we explicitly fix

| Failure | Control |
|---|---|
| Unbounded subagents | `MAX_AGENT_DEPTH = 1`, orchestrator-owned spawn |
| Context duplication | Shared immutable snapshot + repo index cache |
| Self-judging coder | Independent verifier, reviewer, judge |
| Lost session memory | `task_state.json` + `events.jsonl` + artifacts |
| Static dumped rules | Discovery, scope, dedupe, path filter |
| Blind retries | Failure classification then re-plan |
| Token blowups | Token, tool, iteration, runtime budgets |
| Fake confidence | Evidence hierarchy; no evidence ⇒ no high-confidence finding |

## Context retrieval

Exact symbols → semantic/token overlap → one-hop dependencies → tests → rules → compress into a snapshot. Score must usually be ≥ 75 before coding.

## Caches

- Repository cache: tree, symbols, graph (long-lived under `harness/cache`)  
- Task context cache: issue, plan, snapshot  
- Agent context: tool observations (short-lived)

Prompt cache shape: static prefix (system, rules, contract, snapshot) + dynamic suffix (diff, test output).
