# bytes2human() does not roll over at exact powers of 1024

`boltons.strutils.bytes2human(1024)` returns `'1024B'` instead of `'1K'`. `bytes2human(1024**2)` gives `'1024K'` instead of `'1M'`, and the same happens for G and T.

Expected: exact powers of 1024 move to the next unit (`'1K'`, `'1M'`, `'1G'`, `'1T'`). Values just below a boundary stay in the smaller unit (`bytes2human(1023) == '1023B'`, `bytes2human(1024**2 - 1, 1) == '1024.0K'`), and negative values keep working.
