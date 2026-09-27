# last() fails when __reversed__ is None

Setting `__reversed__ = None` is the documented way for a class to say it does not support `reversed()`. `more_itertools.last()` breaks on such objects:

```python
class ReversedIsNone:
    __reversed__ = None
    def __iter__(self):
        return iter([1])

more_itertools.last(ReversedIsNone())  # raises instead of returning 1
```

Expected: `last()` falls back to iterating and returns `1`.
