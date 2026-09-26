# LCC harness — Claude

Read `AGENTS.md` and `harness/docs/HANDOFF.md` before continuing work.

Source of truth:

- `harness/state/task_state.json`
- `harness/state/events.jsonl`
- artifacts under `harness/artifacts/`

Do not treat chat history as durable memory. Do not edit `harness/docs/MASTER_PLAN.md` as a live lock file. Append decisions as `DECISION` events.

You may implement code on the current `agent/*` branch only. Never merge.
