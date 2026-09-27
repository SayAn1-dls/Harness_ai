# PRIMARY KEY is not tokenized as one keyword

`sqlparse.parse('PRIMARY KEY')[0].tokens` contains several tokens, unlike other multi-word keywords such as `GROUP BY` and `ORDER BY`.

Expected: a single token with `ttype == Keyword`.
