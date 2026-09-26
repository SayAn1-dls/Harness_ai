# LCC — Autonomous coding-agent harness

We are building a **model-agnostic autonomous software-engineering harness** that turns software tasks into verified code changes by dynamically managing repository context, engineering rules, tools, specialized agents, execution environments, verification, recovery and compute budgets.

The model provides reasoning. This harness provides the engineering environment in which that reasoning becomes reliable.

## Core loop (must work before anything else)

```text
Issue → Context → Plan → Code → Test → Failure → Recovery → Verified patch → Human gate → PR
```

## Five pillars + efficiency

| Pillar | Verb |
|---|---|
| Context | Know |
| Orchestration | Decide |
| Execution | Act |
| Verification | Prove |
| Recovery | Adapt |
| Efficiency | Bound all of the above |

## What this is not

Not a chatbot. Not a pile of unconstrained subagents. Not a UI-first product. Not an autonomous merge bot.

## Quick start

```bash
python -m pip install -e ".[dev]"
lcc init
lcc ingest --id GH-1 -o "Fix add() so 2+3==5"
lcc run
lcc status
lcc handoff
```

### Providers

Copy `.env.example` to `.env` and set `LCC_PROVIDER` to `deepseek` or `gemini`, with `DEEPSEEK_API_KEY` or `GEMINI_API_KEY`. `openai` and `grok` presets also exist. Any OpenAI-compatible gateway works via `LCC_BASE_URL` + `LCC_MODEL` + `LCC_API_KEY`. The default provider is `mock` (offline, keyword heuristics only).

### Benchmark

```bash
lcc bench -p scripted          # offline smoke test of the pipeline
lcc bench -p deepseek          # live run; grades each task with a hidden check
lcc bench -p gemini -t failure_heavy -t missing_context
```

Results go to `benchmarks/results/*.jsonl`: resolved, iterations, tokens, tool calls, runtime, and **verified resolutions per 1M tokens**.

### What "verified" means

Tests pass **and** there is proof the change fixes something: previously failing tests now pass, or new or changed tests fail on the base code and pass with the change. A green suite on unchanged behavior is not verification.

Default merge permission is **denied**. After `HUMAN_REVIEW`, run `lcc approve` then `lcc pr`.

## Source of truth

| Kind | Location |
|---|---|
| Task state | `harness/state/task_state.json` |
| Event log | `harness/state/events.jsonl` |
| Artifacts | `harness/artifacts/` |
| Handoff | `harness/docs/HANDOFF.md` |

Cursor, Claude Code, and Grok are **clients**. They must read/write the same state, not reconstruct work from chat.

## Architecture docs

See `harness/docs/`.
