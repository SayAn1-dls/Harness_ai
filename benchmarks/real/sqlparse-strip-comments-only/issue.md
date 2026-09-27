# strip_comments leaves a comment when the SQL is only comments

`sqlparse.format('--A;--B;', strip_comments=True)` should return `''`, but a comment is left behind. The same goes for `'--A;\n--B;'` and `'/* sql starts here */'`, and for `'/* sql starts here */\n/* or here */'` with `strip_whitespace=True`.

Expected: input made only of comments formats to an empty string. Comments inside real statements keep being stripped as before.
