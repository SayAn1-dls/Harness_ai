# backoff(..., factor=1.0) raises ZeroDivisionError

`boltons.iterutils.backoff(1, 10, factor=1.0)` crashes with `ZeroDivisionError`, although `factor=1.0` (constant backoff) passes validation.

Expected:
- With an explicit count, a constant factor works: `backoff(1, 10, count=5, factor=1.0) == [1.0] * 5`.
- With the count inferred and `start == stop`, a single value is returned: `backoff(5, 5, factor=1.0) == [5.0]`.
- With the count inferred and `start != stop`, a clear `ValueError` is raised (a constant factor can never reach `stop`); this includes `start=0`.

The same applies to `backoff_iter`.
