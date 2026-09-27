# partition_all silently produces bad output when __len__ is wrong

`toolz.partition_all(n, seq)` trusts `len(seq)` to trim the last partition. When a sequence reports a wrong length (for example a `list` subclass whose `__len__` returns one more, or one fewer, than the real number of items), the output is silently wrong.

Expected: `partition_all` detects the inconsistency and raises `LookupError` rather than returning bad data. For example, `list(partition_all(5, seq))` for a 2-item list whose `__len__` says 3 (or 1) must raise `LookupError`. Normal sequences keep working.
