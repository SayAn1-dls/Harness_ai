# interleave_evenly([]) crashes

`list(more_itertools.interleave_evenly([]))` raises `IndexError: list index out of range`. The same happens with `interleave_evenly([], lengths=[])`.

Expected: both return an empty list.
