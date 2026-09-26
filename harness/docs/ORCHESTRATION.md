# Orchestration

Legal states are in `lcc.state_machine.LEGAL_TRANSITIONS`.

Happy path: `RECEIVED → ANALYZING → CONTEXT_BUILDING → RULE_RESOLUTION → [IMPACT_ANALYSIS] → PLANNING → PLAN_VALIDATION → READY_TO_EXECUTE → IMPLEMENTING → TESTING → REVIEWING → JUDGING → VERIFIED → HUMAN_REVIEW → PR_READY`

Failure path: `FAILED → DIAGNOSING → CONTEXT_UPDATE → REPLANNING → IMPLEMENTING`

## Stop conditions

verified success, max iterations, budget exceeded, same failure ≥ 2, decreasing score, missing permission, unsafe action.

## Budgets

Default 100k tokens, 1800s, 5 iterations, tool caps in `lcc.constants`. Orchestrator owns limits.

## Iteration contract

Each recovery writes `iteration_N.json` with observations, failed assumptions, remaining requirements, failure class, and action (`patch|research|re-plan|rollback|retry|escalate`). The next loop does not restart from zero.
