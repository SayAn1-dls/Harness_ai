# Current Handoff

Task:
Bootstrap the LCC autonomous coding-agent harness.

## 1. What are we building?

A model-agnostic harness that turns issues into verified patches with durable state, context snapshots, budgets, and independent verification.

## 2. What has been completed?

- Milestone 0–8 core Python package and CLI  
- Fixture-based evaluation path  
- Docs under `harness/docs/`

## 3. What is currently being worked on?

Running and tightening the first end-to-end mock loop.

## 4. What decisions have already been made?

See `DECISIONS.md`.

## 5. What failed and why?

- none yet (bootstrap)

## 6. What should the next agent do next?

1. Run `pytest` and fix any failures  
2. Point `LCC_PROVIDER` at a real coding model and retry the mini-repo without mock heuristics  
3. Add incremental repo indexing and prompt-cache headers when a live provider is wired
