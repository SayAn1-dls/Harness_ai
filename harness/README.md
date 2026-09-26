# Harness memory

This directory is the durable project memory.

- `docs/` — human-readable architecture (generated from decisions + this repo)  
- `state/` — **source of truth** (`task_state.json`, `events.jsonl`)  
- `artifacts/` — agent outputs  
- `cache/` — repository index  
- `research/` — competitor and pain notes  

Do not let two models concurrently rewrite `docs/MASTER_PLAN.md`. Write events and state; then refresh `docs/HANDOFF.md`.
