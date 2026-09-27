# interpose() on an empty sequence raises StopIteration

`list(toolz.interpose('a', []))` raises `StopIteration` instead of returning `[]`.

Expected: interposing into an empty sequence yields nothing.
