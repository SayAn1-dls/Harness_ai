# Agent specification

Agents are **logical roles**. The orchestrator activates a subset per lane. They cannot spawn children.

| Agent | Writes code? | Typical tools |
|---|---|---|
| Intake | No | read/search |
| Mapper | No | repo_tree |
| Rules | No | none (filesystem discover) |
| Impact | No | find_references |
| Planner | No | none |
| Coder | Yes | read, search, write/patch |
| Verifier | No | run_test, lint, typecheck |
| Security | No | read/diff (risk-triggered) |
| Reviewer | No | read/diff |
| Judge | No | none |
| Recovery | No | none |

Contracts live in `lcc.agents.CONTRACTS`.

## Lanes

- **A trivial:** intake, coder, verifier  
- **B normal:** + mapper, rules, planner, reviewer, judge  
- **C complex:** + impact, security when risk words match  

## Finding schema

Every finding: id, category, severity, confidence, location, evidence, requirement/rule links, agent, iteration.

Severity is not confidence. Judge priority conceptually `severity × confidence × impact × requirement_relevance`, weighted by evidence level (test > static > rule > dataflow > LLM-only).
