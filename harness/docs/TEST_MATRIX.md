# Test matrix

| Case | Expected |
|---|---|
| Mini-repo wrong `add()` | Mock coder patches; unittest passes; HUMAN_REVIEW |
| Illegal state jump | `IllegalTransition` |
| Tool path escape | `ToolError` |
| Search budget | `ToolBudgetExceeded` |
| Agent spawn | `AgentSpawnError` at depth > 1 |
| Rules discovery | AGENTS.md + cursor rules ingested |
| Context gate tiny repo | coding still allowed |
| Repeated test failure | stop `same_failure_repeated` |
| Merge permission | denied |
