"""Tests for the State enum from AC-3.

Locks the 9-member roster, str-mixin behaviour, and exact string values.
"""
from __future__ import annotations

from autofix.workflow import State


_EXPECTED: list[tuple[str, str]] = [
    ("SCANNING", "scanning"),
    ("TRIAGING", "triaging"),
    ("PLANNING", "planning"),
    ("APPLYING", "applying"),
    ("VERIFYING", "verifying"),
    ("DONE", "done"),
    ("RETRY", "retry"),
    ("HUMAN_REVIEW", "human-review"),
    ("FAILED", "failed"),
]


def test_enum_has_exactly_nine_members() -> None:
    """The enum from AC-3 has exactly 9 members."""
    assert len(list(State)) == 9


def test_enum_member_names_and_values_match_spec() -> None:
    """Each member's .name and .value match AC-3's table verbatim."""
    actual = [(m.name, m.value) for m in State]
    assert actual == _EXPECTED


def test_str_mixin_equality() -> None:
    """The str mixin makes member == its string value (used for JSON serialization)."""
    assert State.SCANNING == "scanning"
    assert State.HUMAN_REVIEW == "human-review"
    assert isinstance(State.DONE, str)
