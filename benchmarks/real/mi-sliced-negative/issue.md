# sliced() with a negative size returns a wrong result

`list(more_itertools.sliced('ABCDEFG', -1))` returns `['ABCDEF']`: silently wrong output. `chunked()` already rejects a negative size.

Expected: a negative `n` raises `ValueError`, with and without `strict=True`.
