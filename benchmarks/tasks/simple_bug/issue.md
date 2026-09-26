`stats.mean` is wrong: `mean([2, 4])` returns `6.0` instead of `3.0`.
The mean of an empty sequence should keep raising `ValueError`.
