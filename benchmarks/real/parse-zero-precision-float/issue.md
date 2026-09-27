# {:.0f} does not match numbers formatted with zero precision

`format(12.0, '.0f') == '12'`, yet `parse.parse('a {:.0f} b', 'a 12 b')` does not match. Likewise `parse.parse('foo_{:02.0f}t', 'foo_20t')` fails.

Expected: these parse to `12.0` and `20.0`. `'a 12.0 b'` must *not* match `'a {:.0f} b'`, since a zero-precision float has no decimal point. The same applies to the `F` (Decimal) type.
