# Current Handoff

Task: make the core loop real (Issue → Context → Plan → Code → Test → Failure → Recovery → Verified patch) on DeepSeek or Gemini.
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
- Tests: 29 passing (`pytest`).

## 3. What is currently being worked on?

The first live benchmark run against a real model. It is blocked on an API key (none is configured on this machine).

## 4. What decisions have already been made?

See `DECISIONS.md` (2026-09-26 entries).

## 5. What failed and why?

- The first scripted bench reported 4 tasks as "verified" although the coder changed nothing, because the existing tests already passed. Fixed by the baseline plus fail-to-pass proof requirement.
- Offline scripted bench (`lcc bench -p scripted`): the 3 scripted tasks pass (simple_bug; failure_heavy via recovery on attempt 2; ambiguous escalates). The 4 unscripted tasks correctly fail, since the mock cannot solve them.

## 6. What should the next agent do next?

1. Put a key in `.env` (see `.env.example`), then run `lcc bench -p deepseek` (or `-p gemini`). Target: at least 5 of 7 resolved, failure_heavy resolved, ambiguous escalated.
2. Read `benchmarks/results/*.jsonl` and `harness/artifacts/coder_*_transcript.json` in the kept workspaces for failures. Tune prompts and tools from the transcripts, not by guessing.
3. Check whether Gemini's OpenAI-compatible endpoint accepts `content: null` on assistant tool-call messages and the tool schemas as sent. If not, adjust `ChatResult.assistant_message` and `ToolSpec` schemas.
4. Record `tokens_cached` from the live runs, and keep the coder's system prefix byte-stable to maximize implicit prefix caching.
5. Later: incremental repo index (mtime/hash cache), a history engine (git log/blame retrieval), and resuming a task mid-state (`lcc run` currently assumes RECEIVED).
