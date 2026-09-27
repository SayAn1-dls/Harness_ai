# split(iterator, maxsplit=0) returns the iterator object

With `from boltons.iterutils import split`, `values = [1, None, 2]`:

- `split(values, maxsplit=0)` returns `[[1, None, 2]]` (correct).
- `split(iter(values), maxsplit=0)` returns a list containing the *iterator object itself*.

Expected: with `maxsplit=0`, both return `[[1, None, 2]]`, a single list of the input's values.
