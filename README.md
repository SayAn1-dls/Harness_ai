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

## Evaluation quickstart (standard Makefile interface)

```bash
git clone <this repo> && cd LCC
export AI_API_KEY="<PROVIDED_API_KEY>"
make setup      # venv + dependencies (Python >= 3.11, git)
make run        # evaluation mode: waits for an issue
make test       # unit tests + offline end-to-end benchmark (no key needed)
make clean      # remove generated artefacts
```

`make run` starts an interactive session. Give it the issue in any of these forms:

- a GitHub issue URL (`https://github.com/owner/repo/issues/123`). The issue and its comments are fetched, and the repository is cloned into `workspaces/`.
- a path to an issue file (`.md` or `.txt`).
- the issue text itself, pasted and ended with a line containing only `END`. If there is no `Repository: <url|path>` line, the session asks for the repository.

Non-interactive forms:

```bash
make run ISSUE=https://github.com/owner/repo/issues/123
make run ISSUE=issue.md REPO=/path/to/repo      # REPO: local path, git URL, or owner/name
make run REPO=owner/name < issue.md
```

For each issue, the harness:

1. prepares an isolated venv for the target repo and installs its dependencies.
2. runs Issue → Context → Plan → Code → Test → Recover on an `agent/<task>` branch, with live progress.
3. prints the diff and the verdict.
4. writes `outputs/<task>.patch` and `outputs/<task>.json` (status, iterations, tokens, tool calls, runtime).

A verified fix is committed on the task branch. Nothing is ever merged or pushed.

### Model configuration

The model is defined in [`lcc.config.toml`](lcc.config.toml): provider, model, temperature `0.0`, seed, and optional fast/strong routing. The credential is read **only** from `AI_API_KEY` at runtime and is never stored in any file.

With `provider = "auto"`, the harness picks the endpoint from the key format:

| Key prefix | Provider |
|---|---|
| `AIza` | Gemini |
| `sk-ant-` | Anthropic |
| `sk-or-` | OpenRouter |
| `gsk_` | Groq |
| `xai-` | xAI |
| `sk-proj-` | OpenAI |

A plain `sk-` key is checked against DeepSeek, then OpenAI.

To use a prescribed model, set `model` (and `provider` or `base_url` if needed) in the config, or override with `LCC_PROVIDER`, `LCC_MODEL` or `LCC_BASE_URL` without editing source. Any OpenAI-compatible endpoint works via `base_url`.

- **Text only:** all model traffic is text chat-completions with function calling.
- **Diagnostics:** `make doctor` checks python, git, the config, whether the key is present, and which provider it resolves to.

### Developer CLI

```bash
lcc ingest --id GH-1 -o "Fix add() so 2+3==5" && lcc run && lcc status && lcc handoff
```

### Benchmark

```bash
lcc bench -p scripted          # offline smoke test of the pipeline
make eval                      # live run (AI_API_KEY); grades each task with a hidden check
make eval TASK="failure_heavy missing_context"
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
