Pages are 1-based, but `paginate(list(range(10)), page=1, per_page=3)` returns `[3, 4, 5]`;
it should return `[0, 1, 2]`. The last page may be partial (`page=4` -> `[9]`), and a page past the end
returns `[]`. `page < 1` should raise `ValueError`.
