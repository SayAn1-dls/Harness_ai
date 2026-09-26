# Context engine

Vector search is **not** the context engine. It is an optional later retrieval.

v1 index:

- file tree (skipped build/venv/git)  
- language counts  
- symbols (Python AST + regex for other languages)  
- import graph and reverse graph  
- tests and entry points  
- PageRank-style repository map under a token budget  

`retrieve()` ranks files by token overlap with the issue, name matches, tests, and injects extra files. Output is a `ContextSnapshot` with packets, score breakdown, and id `ctx_*`.

Shared snapshot rule: agents consume the snapshot; they do not rescan the repository independently inside a task.
