import pytest
from stats import mean

def test_mean_values():
    assert mean([1, 2, 3, 4]) == 2.5
    assert mean([5]) == 5
    assert mean((2, 4)) == 3

def test_mean_empty():
    with pytest.raises(ValueError):
        mean([])
