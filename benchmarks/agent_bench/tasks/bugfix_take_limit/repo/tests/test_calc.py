from src.calc import take_limit


def test_take_limit_returns_exact_limit():
    assert take_limit([1, 2, 3, 4], 2) == [1, 2]


def test_take_limit_zero():
    assert take_limit([1, 2, 3], 0) == []


def test_take_limit_overflow():
    assert take_limit([1, 2], 5) == [1, 2]
