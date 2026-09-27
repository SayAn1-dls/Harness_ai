# findall() ignores case_sensitive=True

`[r.fixed[0] for r in parse.findall('x({})x', 'X(hi)X', case_sensitive=True)]` returns `['hi']`, because the `case_sensitive` argument has no effect.

Expected: `[]` when `case_sensitive=True`. The default (case-insensitive) search still returns `['hi']`.
