# Comparing complement sets with <= / >= raises AttributeError

With `from boltons.setutils import complement`:

```python
cab = complement('ab')
cab <= complement('a')   # AttributeError
complement('a') >= cab   # AttributeError
```

Expected: comparisons follow set semantics, where `complement(X) <= complement(Y)` holds exactly when Y is a subset of X. So `cab <= complement('a')` is True, `complement('a') <= cab` is False, `complement('a') >= cab` is True, and `cab <= cab` and `cab >= cab` are True. The same kind of comparison between two ordinary included sets wrapped by the class must not crash either.
