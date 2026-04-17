"""Tests for autofix_next.events.schema and package layout.

Covers:
  AC #1  — all subpackages under autofix_next/ have __init__.py
  AC #13 — NEW_EVENT_NAMES is the exact 6-camelCase set and does not
           collide with any legacy snake_case event name written by
           autofix/runtime/dynos.py:log_event.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "autofix_next"

EXPECTED_SUBPACKAGES = [
    "events",
    "invalidation",
    "parsing",
    "indexing",
    "analyzers",
    "evidence",
    "llm",
    "funnel",
    "telemetry",
    "cli",
]


def test_all_subpackages_have_init() -> None:
    """AC #1: every subpackage in design-decisions §1 has an __init__.py."""
    assert PACKAGE_ROOT.is_dir(), f"{PACKAGE_ROOT} missing"
    root_init = PACKAGE_ROOT / "__init__.py"
    assert root_init.is_file(), "autofix_next/__init__.py missing"
    for sub in EXPECTED_SUBPACKAGES:
        init_path = PACKAGE_ROOT / sub / "__init__.py"
        assert init_path.is_file(), f"{init_path} missing"


def test_new_event_names_are_exact_set() -> None:
    """AC #20: NEW_EVENT_NAMES is exactly the 7-camelCase set after
    task-003's ``InvalidationComputed`` extension (AC #19)."""
    from autofix_next.events import schema as events_schema

    expected = frozenset(
        {
            "ScanStarted",
            "SymbolIndexed",
            "EvidencePacketBuilt",
            "LLMCallGated",
            "SARIFEmitted",
            "ScanCompleted",
            "InvalidationComputed",
        }
    )
    assert hasattr(events_schema, "NEW_EVENT_NAMES"), (
        "autofix_next.events.schema must expose NEW_EVENT_NAMES"
    )
    assert isinstance(events_schema.NEW_EVENT_NAMES, (set, frozenset))
    assert frozenset(events_schema.NEW_EVENT_NAMES) == expected


def test_new_event_names_includes_invalidation_computed() -> None:
    """AC #19: ``InvalidationComputed`` is in ``NEW_EVENT_NAMES`` and the
    set has exactly 7 names after the task-003 extension."""
    from autofix_next.events import schema as events_schema

    assert "InvalidationComputed" in events_schema.NEW_EVENT_NAMES
    assert len(events_schema.NEW_EVENT_NAMES) == 7


def test_changeset_gains_is_fresh_instance_field() -> None:
    """AC #1: ``ChangeSet`` gains an ``is_fresh_instance: bool = False``
    field; existing 2-positional construction continues to work."""
    from autofix_next.events.schema import ChangeSet

    # 2-positional construction (existing API) must still yield a valid
    # instance; the new field defaults to False.
    cs_default = ChangeSet(("a.py",), "diff-head1")
    assert cs_default.paths == ("a.py",)
    assert cs_default.watcher_confidence == "diff-head1"
    assert getattr(cs_default, "is_fresh_instance", None) is False

    # Keyword-only construction without the new field stays valid.
    cs_kw_default = ChangeSet(paths=(), watcher_confidence="full-sweep")
    assert cs_kw_default.is_fresh_instance is False

    # Explicit True is honored.
    cs_true = ChangeSet(
        paths=("b.py",),
        watcher_confidence="full-sweep-fallback",
        is_fresh_instance=True,
    )
    assert cs_true.is_fresh_instance is True


def test_new_event_names_no_collision_with_legacy() -> None:
    """AC #13: new camelCase names must not collide with any snake_case
    name that autofix/runtime/dynos.py:log_event historically emits."""
    from autofix_next.events import schema as events_schema

    # All new event names must be camelCase (start with upper-case letter)
    # and must contain no underscores — legacy names are snake_case.
    for name in events_schema.NEW_EVENT_NAMES:
        assert isinstance(name, str) and name, f"event name must be non-empty: {name!r}"
        assert name[0].isupper(), f"event name not camelCase: {name!r}"
        assert "_" not in name, f"event name contains underscore (legacy style): {name!r}"
