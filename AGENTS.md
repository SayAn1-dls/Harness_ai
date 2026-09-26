We are building a model-agnostic autonomous software-engineering harness that turns software tasks into verified code changes by dynamically managing repository context, engineering rules, tools, specialized agents, execution environments, verification, recovery and compute budgets.

# Laws

1. Conversation is temporary reasoning. `harness/state/task_state.json` plus the event log and artifacts are durable truth.
2. Only the orchestrator writes task state. Agents write artifacts; the orchestrator appends events.
3. Do not rewrite `MASTER_PLAN.md` as a merge battlefield. Update machine state, then regenerate human docs.
4. `MAX_AGENT_DEPTH = 1`. Agents must not spawn agents. The orchestrator owns activation.
5. Never modify `main` / `master` / `trunk`. Work on `agent/<task-id>` and emit a PR after human approval.
6. Coder saying "done" is not verification. Independent tests, lint, and evidence are required.
7. No evidence, no high-confidence finding.
8. Do not start coding if the context snapshot score is below 75 unless the repository is tiny.
9. Respect token, tool, iteration, and runtime budgets.
10. Default GitHub permissions exclude merge.

# Pipeline

Issue → Intake → Context → Rules → Impact (if needed) → Plan → Implement → Verify → Review → Judge → Recover or Human gate → PR

# Handoff

Always leave `harness/docs/HANDOFF.md` answering: what we are building, completed, current, decisions, failures, next steps.
