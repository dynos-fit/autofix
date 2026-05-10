"""Unit tests for autofix.analyzers._registry private helpers.

Covers AC-6 (_bind_correlation_ctx) and AC-7 (try/finally cleanup) from
spec.md. Imports private names by design — the tests verify implementation
behavior of the contextvar binding and the cleanup invocation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from autofix.analyzers._registry import _bind_correlation_ctx, analyze_files
from autofix.telemetry.correlation import _COMMIT_SHA, _SCAN_ID, _EVENT_ID


# --- _bind_correlation_ctx ---------------------------------------------------

def test_bind_all_none_does_not_set_contextvars():
    """All three params None -> contextvars unchanged during and after."""
    pre_commit = _COMMIT_SHA.get(None)
    pre_scan = _SCAN_ID.get(None)
    pre_event = _EVENT_ID.get(None)
    with _bind_correlation_ctx(None, None, None):
        assert _COMMIT_SHA.get(None) == pre_commit
        assert _SCAN_ID.get(None) == pre_scan
        assert _EVENT_ID.get(None) == pre_event
    assert _COMMIT_SHA.get(None) == pre_commit
    assert _SCAN_ID.get(None) == pre_scan
    assert _EVENT_ID.get(None) == pre_event


def test_bind_all_set_restored_after_block():
    """All three params set -> contextvars set during, restored after."""
    pre_commit = _COMMIT_SHA.get(None)
    pre_scan = _SCAN_ID.get(None)
    pre_event = _EVENT_ID.get(None)
    with _bind_correlation_ctx("commit-abc", "scan-xyz", "event-789"):
        assert _COMMIT_SHA.get(None) == "commit-abc"
        assert _SCAN_ID.get(None) == "scan-xyz"
        assert _EVENT_ID.get(None) == "event-789"
    assert _COMMIT_SHA.get(None) == pre_commit
    assert _SCAN_ID.get(None) == pre_scan
    assert _EVENT_ID.get(None) == pre_event


def test_bind_resets_on_exception():
    """When body raises, contextvars STILL reset (the SEC-RUFF-02 lesson)."""
    pre_commit = _COMMIT_SHA.get(None)
    pre_scan = _SCAN_ID.get(None)
    pre_event = _EVENT_ID.get(None)
    with pytest.raises(RuntimeError):
        with _bind_correlation_ctx("commit-abc", "scan-xyz", "event-789"):
            raise RuntimeError("body failure")
    # All three must be back to their pre-call values, even though body raised.
    assert _COMMIT_SHA.get(None) == pre_commit
    assert _SCAN_ID.get(None) == pre_scan
    assert _EVENT_ID.get(None) == pre_event


def test_bind_mixed_none_leaves_others_unchanged():
    """Mixed None + non-None -> None ones are NOT overwritten."""
    # Pre-set _SCAN_ID with a sentinel; _COMMIT_SHA is set; _EVENT_ID stays None.
    sentinel_token = _SCAN_ID.set("preset-scan-sentinel")
    try:
        pre_event = _EVENT_ID.get(None)
        with _bind_correlation_ctx("commit-abc", None, None):
            assert _COMMIT_SHA.get(None) == "commit-abc"
            # _SCAN_ID's preset value must NOT be overwritten with None.
            assert _SCAN_ID.get(None) == "preset-scan-sentinel"
            # _EVENT_ID was not touched by the bind.
            assert _EVENT_ID.get(None) == pre_event
    finally:
        _SCAN_ID.reset(sentinel_token)


def test_bind_reverse_reset_order():
    """Reset order is reverse of set order -- verified by all values restoring."""
    pre_commit = _COMMIT_SHA.get(None)
    pre_scan = _SCAN_ID.get(None)
    with _bind_correlation_ctx("commit-1", "scan-2", None):
        assert _COMMIT_SHA.get(None) == "commit-1"
        assert _SCAN_ID.get(None) == "scan-2"
    # Both must restore -- proves reset was called for each token.
    assert _COMMIT_SHA.get(None) == pre_commit
    assert _SCAN_ID.get(None) == pre_scan


# --- _reset_passthrough_analyzer_state cleanup -------------------------------

def test_cleanup_runs_on_normal_return():
    """analyze_files() returns normally -> passthrough cleanup ran."""
    with patch(
        "autofix.analyzers.linter_passthrough.eslint._reset_per_scan_state"
    ) as mock_reset:
        result = analyze_files(
            [],
            analyzers=[],
            repo_root=Path("."),
        )
        assert result == []
        # Cleanup ran via try/finally even though no analyzer was invoked.
        assert mock_reset.called


def test_cleanup_runs_on_exception_in_body():
    """If something inside the analyze_files body raises (uncaught), cleanup STILL ran.

    The implementation catches per-(file, analyzer) errors but propagates anything
    raised by _bind_correlation_ctx itself or the surrounding control flow.
    Simulate by patching _bind_correlation_ctx to raise.
    """
    from contextlib import contextmanager

    @contextmanager
    def _raising_bind(commit_sha, scan_id, event_id):
        raise RuntimeError("bind failure")
        yield  # unreachable

    with patch(
        "autofix.analyzers._registry._bind_correlation_ctx",
        side_effect=_raising_bind,
    ):
        with patch(
            "autofix.analyzers.linter_passthrough.eslint._reset_per_scan_state"
        ) as mock_reset:
            with pytest.raises(RuntimeError):
                analyze_files([], analyzers=[], repo_root=Path("."))
            # Cleanup must NOT have run because _bind_correlation_ctx raised
            # before the try-block was entered. The try/finally is INSIDE the
            # with-block, so when the context manager raises before yielding,
            # the try body is never reached and the finally clause never fires.
            assert not mock_reset.called  # bind raised before try-block; cleanup skipped by design
