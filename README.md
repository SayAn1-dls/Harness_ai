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
make test       # unit tests + offline end-to-end benchmark on all 7 tasks (no key needed)
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
make run ISSUE=issue.md REPO=https://github.com/o/r BASE=<sha>   # pin the base commit
```

The base commit can also be given as `REPO=<url>@<sha>` or as a `Base commit: <sha>` line in the issue.

A local `REPO` that is a git repository root is worked on in place, on an `agent/*` branch. Your branch and uncommitted changes are restored afterwards. Any other folder is copied into `workspaces/` first.

For each issue, the harness:

1. prepares an isolated venv for the target repo and installs its dependencies.
2. runs Issue → Context → Plan → Code → Test → Recover on an `agent/<task>` branch, with live progress.
3. prints the diff and the verdict.
4. writes `outputs/<task>.patch` and `outputs/<task>.json` (status, iterations, tokens, tool calls, runtime).

A verified fix is committed on the task branch. Nothing is ever merged or pushed.

### Model configuration

The model is defined in [`lcc.config.toml`](lcc.config.toml): provider, model, temperature `0.0`, seed, and optional fast/strong routing. The credential is read **only** from `AI_API_KEY` at runtime and is never stored in any file.

The final evaluation uses **DeepSeek and Qwen** models. With `provider = "auto"`:

| Key | Provider | Default model (fallbacks when the account lacks it) |
|---|---|---|
| plain `sk-…` accepted by DeepSeek | `deepseek` | `deepseek-chat` → `deepseek-reasoner` |
| plain `sk-…` accepted by DashScope intl / China | `qwen` / `qwen-cn` | `qwen3-coder-plus` → `qwen-plus` → `qwen-max` → `qwen-turbo` |
| `sk-or-` | OpenRouter | |
| `AIza` | Gemini | |
| `sk-ant-`, `sk-proj-`, `gsk_`, `xai-` | Anthropic, OpenAI, Groq, xAI | |

A plain `sk-` key is identified with `GET /models`, which spends no tokens. It is sent only to DeepSeek and DashScope, never to other vendors.

Before any work, `make run`, `make doctor` and `make eval` send one tiny completion (the preflight). A rejected key (401), empty balance (402), denied access (403) or unknown model (404) is reported in seconds and stops the run. The same errors mid-run stop the benchmark instead of failing every task.

To pin a model, set `model` in the config or `LCC_MODEL` (for example `deepseek-reasoner` or `qwen-max`). `LCC_PROVIDER` and `LCC_BASE_URL` override the rest without editing source.

**Your own LLM:** any OpenAI-compatible server works through `base_url`, for example Qwen on vLLM or Ollama at `http://localhost:8000/v1`. The adapter drops parameters a server rejects (`enable_thinking`, `response_format`, `parallel_tool_calls`, `seed`) and retries. If the server has no function calling, it describes the tools in the prompt and parses `<tool_call>{…}</tool_call>` blocks from the reply. `<think>` reasoning is stripped.

- **Text only:** all model traffic is text chat-completions with function calling.
- **Diagnostics:** `make doctor` checks python, git, the config and the key, then makes one tiny model call.

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

### How the coder works

The coder follows a senior-engineer routine: locate the code, reproduce the bug, fix the root cause, add a regression test, verify, then finish. Its tools:

- **Navigation:** `search_code` (literal or regex, with a path glob, respects `.gitignore`), `find_symbol`, `find_references`, `repo_tree`, `read_file`, `git_history` (log/blame).
- **Editing:** `edit_file` (exact replace; tolerates trailing-whitespace drift and pasted line numbers, points at the closest lines on a miss, supports `replace_all`), `write_file`.
- **Running:** `shell` (to reproduce or inspect; `python` resolves to the target's environment; destructive and git-state commands are blocked), `run_test`.

Tests are detected for Python (pytest or unittest), JavaScript (`npm test`; output from node --test, jest, vitest and mocha is parsed), Go (`go test ./...`) and Rust (`cargo test`). `make run` installs a target's Python and npm dependencies first.

### Token efficiency

Every step of a tool loop re-sends the conversation, so the harness keeps each step small and asks for fewer steps:

- The coder's static prefix (rules, repo map, ranked context excerpts) is byte-identical across steps, so DeepSeek and DashScope prefix caching hits. The `make eval` summary reports `cached_share`.
- Superseded reads of the same lines become one-line stubs. A passing test run is one line; a failing run shows only the failures. Observations are capped, and old ones are compacted past 16k tokens.
- The prompt asks for independent lookups in one turn (parallel tool calls), and the agent is warned 3 steps before its step limit.
- JSON agents use `response_format=json_object` where the server supports it, which avoids repair calls. Simple (lane A) tasks skip the planner call.
- On a scripted 16-step session with identical steps, prompt tokens fell from 120,783 to 97,424 (−19%) against the previous version.

### What "verified" means

Two conditions must hold:

- **Pass-to-pass:** no test that passed before the change fails after it. Tests that already failed at baseline (flaky, environment-bound or unrelated) do not block, and they are listed in the report.
- **Fail-to-pass:** a test that failed at baseline now passes, or a new or changed test fails on the base code and passes with the change.

A green suite on unchanged behavior is not verification. If the full suite exceeds `run.test_timeout`, the tests related to the change are used instead.

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
