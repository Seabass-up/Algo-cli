import pytest

from calculator import add, average


def test_adds_numbers():
    assert add(2, 3) == 5


def test_average_even_result():
    assert average([2, 4, 6]) == 4


def test_average_fractional_result():
    assert average([1, 2]) == 1.5


def test_average_rejects_empty_list():
    with pytest.raises(ValueError):
        average([])
