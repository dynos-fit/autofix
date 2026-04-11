import pytest

from src.config import parse_port


def test_parse_port_valid():
    assert parse_port("8080") == 8080


def test_parse_port_invalid():
    with pytest.raises(ValueError):
        parse_port("abc")
