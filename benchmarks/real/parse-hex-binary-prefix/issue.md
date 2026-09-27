# Hex value that looks like a binary prefix is misparsed

`parse.parse('a {:x} b', 'a 0B b')[0]` should be `11` (hexadecimal B), but `'0B'` is treated as a binary prefix with no digits.

Expected: `'a 0B b'` with `{:x}` gives `11`. An explicit base prefix followed by digits still overrides the base: `'a 0B1 b'` with `{:x}` gives `1`.
