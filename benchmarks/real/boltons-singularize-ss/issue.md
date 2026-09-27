# singularize() mangles words ending in 'ss'

`boltons.strutils.singularize('glass')` returns `'glas'`. Likewise `'boss'` becomes `'bos'` and `'kiss'` becomes `'kis'`, and `'class'`, `'address'` and `'business'` are corrupted the same way.

Expected: words ending in a double `s` are already singular and come back unchanged, with case preserved (`'Glass'` stays `'Glass'`). Plurals such as `'glasses'` → `'glass'` keep working.
