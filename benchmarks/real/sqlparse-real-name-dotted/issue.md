# get_real_name() wrong for names with more than two dotted parts

For a fully qualified identifier, `get_real_name()` returns an intermediate component:

```python
ident = sqlparse.parse('db.schema.tbl.col')[0].tokens[0]
ident.get_real_name()   # 'schema', expected 'col'
```

Expected: `get_real_name()` and `get_name()` return `'col'`, and `get_parent_name()` stays `'db'`. For `x.y.z AS w`, the real name is `'z'` and the alias is `'w'`. Two-part names keep working.
