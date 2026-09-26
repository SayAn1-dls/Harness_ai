# Decisions

Append-only product decisions. Prefer `DECISION` events in `events.jsonl` for task-local choices.

- 2026-09-26: Python 3.11+ harness; pydantic schemas; filesystem event sourcing.  
- 2026-09-26: Model adapter protocol; mock default; OpenAI-compatible optional.  
- 2026-09-26: tree-sitter not required for v1; Python `ast` + regex. Embeddings secondary.  
- 2026-09-26: unittest for target repos so verification does not depend on pytest being installed in the target.  
- 2026-09-26: Human approval required before `gh pr create`.
