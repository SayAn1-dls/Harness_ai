# intfmt crashes on numeric strings in an integer column

```python
tabulate([[82642], ['1500'], [2463]], intfmt=',', tablefmt='plain')
```
raises `ValueError: Cannot specify ',' with 's'.`

Expected output (string cells are printed as they are, real ints are formatted):
```
82,642
  1500
 2,463
```
