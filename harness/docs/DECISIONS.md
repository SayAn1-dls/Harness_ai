# Decisions

Append-only product decisions. Prefer `DECISION` events in `events.jsonl` for task-local choices.

- 2026-09-26: Python 3.11+ harness; pydantic schemas; filesystem event sourcing.  
- 2026-09-26: Model adapter protocol; mock default; OpenAI-compatible optional.  
- 2026-09-26: tree-sitter not required for v1; Python `ast` + regex. Embeddings secondary.  
- 2026-09-26: unittest for target repos so verification does not depend on pytest being installed in the target.  
- 2026-09-26: Human approval required before `gh pr create`.
- 2026-09-26: LCC is its own git repo (the enclosing `~/.git` is the user's home dir and must not be touched). Work on `agent/core-loop`.
- 2026-09-26: Real backends are DeepSeek and Gemini via their OpenAI-compatible chat-completions + function-calling endpoints (one adapter, presets in `lcc.model.PRESETS`). A missing API key is an error; there is no silent fallback to mock.
- 2026-09-26: The coder is a tool-using loop (`lcc.agent_loop`), not single-shot JSON. Observations are bounded (6k chars); identical calls are warned then stopped; tokens and runtime are metered per model call (`MODEL_CALL` events).
- 2026-09-26: Scope policy: forbidden paths (`harness/**`, `.git/**`, plan.forbidden_files) are hard-blocked; writes outside plan.allowed_files are allowed but recorded as `scope_expansions` and shown to the reviewer (`scope_mode="strict"` refuses them instead).
- 2026-09-26: Verification needs proof, not just a green suite: a baseline run is recorded before the first attempt; a pass requires either previously failing tests now passing, or changed/new tests that fail on the base code (sources temporarily reverted) and pass with the change. No changes means not verified. Zero collected tests is not a pass.
- 2026-09-26: Intake escalates on `blocking_ambiguities` (a question only the requester can answer) instead of guessing.
- 2026-09-26: Context gate is enforced for repos with more than 8 files: below 75 after expansion and a read-only context-agent pass means ESCALATED/missing_context. The snapshot score was recalibrated to measure relevance (the old score rewarded file count). Known weakness: `relevant_files` is self-referential (top-ranked vs selected).
- 2026-09-26: Compaction is deterministic: once an agent's working context exceeds 24k tokens, old tool observations become one-line stubs (the 6 most recent are kept). No summarization model call.
- 2026-09-26: Routing is opt-in: `LCC_MODEL_FAST` serves intake/recovery/review/context; `LCC_MODEL_STRONG` serves planner/coder on Lane C or after 2+ failed attempts.
