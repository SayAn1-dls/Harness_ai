# Lowercase 'as' in CREATE TABLE ... AS SELECT disables function grouping

SQL keywords are case-insensitive, but:

```python
p = sqlparse.parse('create table tbl1 as select coalesce(t1.col1, 0) as col1 from t1')[0]
```

This does not group `coalesce(t1.col1, 0) as col1` as an aliased identifier whose first token is a `sql.Function`. With uppercase `AS` after the table name it works.

Expected: `p.tokens[10].get_alias() == 'col1'` and `isinstance(p.tokens[10].tokens[0], sql.Function)`, whatever the case of `as`.
