"""VerifyResult shape + immutability (ARCH-011 AC-3)."""
from __future__ import annotations

from pathlib import Path

import pytest

from autofix.workflow import VerifyResult


def test_namedtuple_fields_in_order() -> None:
    expected = (
        "test_passed",
        "test_runner",
        "test_command",
        "test_log_path",
        "new_finding_count",
        "unresolved_finding_ids",
        "post_finding_ids",
        "regressed",
    )
    assert VerifyResult._fields == expected


def test_result_is_immutable() -> None:
    r = VerifyResult(
        test_passed=True,
        test_runner="pytest",
        test_command=("pytest", "-x"),
        test_log_path=Path("/tmp/x.log"),
        new_finding_count=0,
        unresolved_finding_ids=frozenset(),
        post_finding_ids=(),
        regressed=False,
    )
    with pytest.raises(AttributeError):
        r.test_passed = False  # type: ignore[misc]


def test_unresolved_is_frozenset() -> None:
    r = VerifyResult(
        test_passed=None,
        test_runner=None,
        test_command=None,
        test_log_path=None,
        new_finding_count=0,
        unresolved_finding_ids=frozenset({"a"}),
        post_finding_ids=("a",),
        regressed=False,
    )
    assert isinstance(r.unresolved_finding_ids, frozenset)
