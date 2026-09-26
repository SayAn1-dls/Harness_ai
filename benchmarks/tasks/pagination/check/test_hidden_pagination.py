import pytest
from paging import paginate

def test_pages():
    items = list(range(10))
    assert paginate(items, 1, 3) == [0, 1, 2]
    assert paginate(items, 2, 3) == [3, 4, 5]
    assert paginate(items, 4, 3) == [9]
    assert paginate(items, 5, 3) == []

def test_bad_page():
    with pytest.raises(ValueError):
        paginate([1], 0, 3)
