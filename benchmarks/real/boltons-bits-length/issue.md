# Bits accepts values that do not fit in the declared length

`boltons.mathutils.Bits(4, 2)` is accepted and produces a 3-bit value (`'100'`) while declaring a length of 2, which breaks indexing and round-tripping. `Bits(1, 0)` is accepted too.

Expected: a value that cannot be represented in `len_` bits raises `ValueError`. The largest value that fits keeps working, for example `Bits(3, 2).as_bin() == '11'`.
