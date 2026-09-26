`word_count("state-of-the-art design")` returns 2 but our editors count hyphen-separated parts as
separate words, so it should return 5. Hyphens at the edges or doubled hyphens must not create empty words:
`word_count("-well--known-")` is 2.
