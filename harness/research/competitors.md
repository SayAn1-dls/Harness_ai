# Competitors (public behavior, not claimed private scores)

- **CodeRabbit:** intent + environment + conversation; graphs; filtering low-value comments; post-generation verification.  
- **Qodo:** context engine, rules lifecycle, mixture of agents, judge.  
- **SWE-agent:** ACI quality dominates raw model choice.  
- **OpenHands:** agent vs runtime split, events, sandbox.  
- **Aider:** repo map + graph ranking + token budget.  
- **GitHub Copilot coding agent:** scoped instructions, AGENTS.md, branch/PR loop.  
- **Cursor / Claude Code:** rules + skills; weak continuity if chat-only.

We combine these primitives and add hard budgets, shared snapshots, and event-sourced handoff.
