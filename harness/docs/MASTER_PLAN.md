# Master plan

We are building a model-agnostic autonomous software-engineering harness that turns software tasks into verified code changes by dynamically managing repository context, engineering rules, tools, specialized agents, execution environments, verification, recovery and compute budgets.

## Milestones

0. Architecture specification — this tree  
1. Task/state engine — **implemented** (`lcc.schemas`, `lcc.store`, `lcc.state_machine`)  
2. Repository intelligence — **implemented** (`lcc.context_engine`)  
3. Rules engine — **implemented** (`lcc.rules_engine`)  
4. Context engine snapshots/scoring — **implemented**  
5. Agent runtime / tools / model adapter — **implemented**  
6. Orchestrator, budgets, recovery — **implemented**  
7. Coding workflow — **implemented** (`lcc run`)  
8. Independent review + judge — **implemented**  
9. Caching, routing, compression — partial (static/dynamic prompt split documented; routing env-based)  
10. Evaluation harness — **implemented** (`lcc.eval`) plus fixture benchmark  

## Do not build first

Fancy UI, Slack, 15 providers, cross-repo reasoning, autonomous merge, huge vector DB, dozens of always-on agents, self-learning rules, fine-tuning.

## Single source of truth

If information matters for another agent to continue, it must exist outside the conversation.
