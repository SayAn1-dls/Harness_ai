# Current Handoff

Task: make the harness reliable and token-efficient on the evaluation models (DeepSeek and Qwen, announced by the organisers).
Branch: `agent/core-loop` (LCC's own git repo). Plan: `~/.claude/plans/pasted-content-id-7bfd-yes-i-snappy-turtle.md`.

## 1. What are we building?

A model-agnostic autonomous software-engineering harness: it turns an issue into a verified patch on an `agent/*` branch, with durable state, bounded context, independent verification, recovery, and token/tool budgets.

## 2. What has been completed?

- Model layer: `chat()` with native tool calls, usage accounting, retries; presets `deepseek`, `gemini`, `openai`, `grok`; `ScriptedProvider` for offline tests (`src/lcc/model.py`).
- Metering: every call charges `task.budget` and appends a `MODEL_CALL` event (`src/lcc/metering.py`).
- ACI tool loop with bounded observations, a repeat-call guard, budget stops, and compaction (`src/lcc/agent_loop.py`). Tools add `edit_file` (unique match plus syntax check), `find_symbol`/`find_references`/`get_repo_map`, and a scope policy (`src/lcc/tools.py`).
- Agents: the coder is a tool loop (static prefix + dynamic suffix prompt); a read-only context agent; intake escalates on blocking ambiguity; recovery sees the diff and previous attempts (`src/lcc/agents.py`).
- Orchestrator: enforced gates; baseline test run; fail-to-pass proof; `IterationRecord` history in `task_state.json` fed into the next attempt; clean stop conditions; commits verified work on the task branch (`src/lcc/orchestrator.py`).
- Opt-in fast/strong routing (`src/lcc/routing.py`). The global score is computed from recorded evidence (`src/lcc/eval.py`).
- Benchmark: 7 tasks with hidden checks (`benchmarks/tasks/`). Each check was validated to fail on the original code and pass with a reference fix. Run with `lcc bench`.
- Tests: 90 passing (macOS and Linux container), CI on 3.11/3.12/3.13.
- Real benchmark: 20 validated real bug fixes (`benchmarks/real/`, `make bench-check`: valid 20/20, oracle 20/20, do-nothing 0/20).
- Security: credential-free child environments, a git guard, and an optional Docker sandbox (`sandbox = "docker"`). (`pytest`). The offline benchmark resolves all 7 tasks, each graded by its hidden check (`make test`).
- DeepSeek and Qwen (DashScope international and China) presets. The auto-detect probe is limited to those vendors. A preflight check and model fallback run before any work, and account errors are fatal. The provider adapts to rejected parameters and falls back to prompt-described tools for servers without function calling.
- Verification based on test sets (pass-to-pass against baseline failures), with a targeted-test fallback on timeout. Base-commit pinning. Local non-root folders are copied. npm, Go and Cargo test paths.
- Token savings in the loop: superseded reads become stubs, test output is condensed, schemas are smaller, and a low-steps warning is sent (−19% prompt tokens on a scripted 16-step session).
- Auto mode: repo link only → discover (failing tests, ruff defect rules, model audit) → verified fixes → one PR each via `gh` (fork when there is no push access), never merged (`src/lcc/discover.py`, `src/lcc/github_pr.py`, `session.auto_fix`). Tested offline against a fake GitHub (`tests/test_auto_mode.py`).
- Hackathon interface: root `Makefile` (`setup`/`run`/`test`/`clean`/`eval`/`doctor`), `lcc.config.toml` (model definition), `AI_API_KEY` only, `lcc start` evaluation session (`src/lcc/session.py`: issue from a URL, file, text or stdin; clone; target venv; live progress; `outputs/<task>.patch` and `.json`).

## 3. What is currently being worked on?

The first live run on DeepSeek or Qwen. No funded key for either is available on this machine. The last DeepSeek key returned 402, and the local `.env` holds a Gemini key.

## 4. What decisions have already been made?

See `DECISIONS.md` (2026-09-26 and 2026-09-27 entries).

## 5. What failed and why?

- The first scripted bench reported 4 tasks as "verified" although the coder changed nothing, because the existing tests already passed. Fixed by the baseline plus fail-to-pass proof requirement.
- Offline scripted bench (`lcc bench -p scripted`): all 7 tasks now have recorded replies and pass their hidden checks (failure_heavy via recovery on attempt 2; ambiguous escalates).
- Every live attempt so far failed on the account, not on the harness: DeepSeek 402 (balance), Gemini 404 (retired model) and 403 (project denied). Those errors are now fatal and reported by the preflight.
- The unpushed 2026-09-26 fixes in the old `~/Desktop/LCC` checkout were lost with that folder; they were re-implemented here on 2026-09-27.

## 6. What should the next agent do next?

1. With a DeepSeek or Qwen key: `make doctor`, then **`make eval-real`** (20 real tasks) and `make eval` (7 toy). Report the real-task resolve rate first.
2. Tune from `harness/artifacts/coder_*_transcript.json` in the kept workspaces. Check `tokens_cached` / `cached_share` in the `make eval` summary; the coder prefix is byte-stable, and a test asserts it.
3. Run `make setup && make test` in a clean Linux container (`python:3.11-slim` + git). This has not been done yet: Docker was not running.
4. Later: an incremental repo index (mtime/hash cache), and resuming a task mid-state (`lcc run` assumes RECEIVED).
