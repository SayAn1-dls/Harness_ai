# one()/only() ignore a falsy custom exception

`more_itertools.one(iterable, too_short=..., too_long=...)` and `more_itertools.only(iterable, too_long=...)` do not raise the exception the caller supplies when its instances are falsy (for example an `Exception` subclass that defines `__bool__` returning `False`): a generic `ValueError` is raised instead.

Also, when `too_long` is supplied, the default error message is still built. It calls `repr()` on the first two items, so an item whose `__repr__` raises hides the caller's exception.

Expected: a supplied `too_short`/`too_long` exception is always raised as given, and the default message (with the item reprs) is only built when no custom exception was supplied.
