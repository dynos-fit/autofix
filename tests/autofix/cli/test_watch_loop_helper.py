"""Unit tests for autofix.cli._watch_loop.run_watch_loop (ARCH-014)."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from autofix.cli._watch_loop import run_watch_loop
from autofix.events.schema import ChangeSet


def _make_session(*next_results: tuple[bool, list[str]]) -> Any:
    """Build a mock session whose next_files() yields the supplied tuples."""
    session = MagicMock()
    session.next_files.side_effect = list(next_results)
    return session


def test_dispatches_once_when_files_arrive_and_once_true() -> None:
    session = _make_session((False, ["sample.py"]))
    dispatcher = MagicMock()
    rc = run_watch_loop(
        session, dispatcher, safety_sweep_seconds=None, once=True
    )
    assert rc == 0
    dispatcher.assert_called_once()
    args, _ = dispatcher.call_args
    assert isinstance(args[0], ChangeSet)
    assert args[0].is_fresh_instance is False
    assert args[0].watcher_confidence == "watch"
    assert args[0].paths == ("sample.py",)


def test_fresh_instance_propagates_to_changeset() -> None:
    session = _make_session((True, ["sample.py"]))
    dispatcher = MagicMock()
    rc = run_watch_loop(
        session, dispatcher, safety_sweep_seconds=None, once=True
    )
    assert rc == 0
    cs = dispatcher.call_args[0][0]
    assert cs.is_fresh_instance is True
    assert cs.watcher_confidence == "full-sweep"


def test_keyboard_interrupt_returns_zero() -> None:
    session = MagicMock()
    session.next_files.side_effect = KeyboardInterrupt()
    dispatcher = MagicMock()
    rc = run_watch_loop(
        session, dispatcher, safety_sweep_seconds=None, once=False
    )
    assert rc == 0


def test_session_close_called_on_success_path() -> None:
    session = _make_session((False, ["sample.py"]))
    dispatcher = MagicMock()
    run_watch_loop(session, dispatcher, safety_sweep_seconds=None, once=True)
    session.close.assert_called_once()


def test_session_close_called_on_keyboard_interrupt() -> None:
    session = MagicMock()
    session.next_files.side_effect = KeyboardInterrupt()
    dispatcher = MagicMock()
    run_watch_loop(session, dispatcher, safety_sweep_seconds=None, once=False)
    session.close.assert_called_once()


def test_no_event_with_once_returns_zero() -> None:
    """Empty batch + once=True → return 0 without dispatching."""
    session = _make_session((False, []))
    dispatcher = MagicMock()
    rc = run_watch_loop(
        session, dispatcher, safety_sweep_seconds=None, once=True
    )
    assert rc == 0
    dispatcher.assert_not_called()


def test_safety_sweep_forces_fresh_dispatch_after_threshold() -> None:
    """With safety_sweep_seconds=0, the very first iteration must dispatch
    a fresh-instance changeset even when next_files returns no events."""
    session = _make_session((False, []))
    dispatcher = MagicMock()
    rc = run_watch_loop(
        session, dispatcher, safety_sweep_seconds=0, once=True
    )
    assert rc == 0
    dispatcher.assert_called_once()
    cs = dispatcher.call_args[0][0]
    assert cs.is_fresh_instance is True


def test_dispatcher_exception_does_not_break_loop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A RuntimeError from the dispatcher must be swallowed; loop continues
    long enough for the next event to be processed. With ``once=True`` and
    a single event, the dispatcher raising still results in clean exit.
    AC-29: the loop logs the exception to stderr."""
    session = _make_session((False, ["a.py"]))
    dispatcher = MagicMock(side_effect=RuntimeError("boom"))
    rc = run_watch_loop(
        session, dispatcher, safety_sweep_seconds=None, once=True
    )
    assert rc == 0
    dispatcher.assert_called_once()
    err = capsys.readouterr().err
    assert "RuntimeError" in err
    assert "boom" in err


def test_dispatcher_keyboard_interrupt_propagates() -> None:
    """KeyboardInterrupt from the dispatcher is caught at the outer
    try/except, not the inner exception handler — return code stays 0."""
    session = _make_session((False, ["a.py"]))
    dispatcher = MagicMock(side_effect=KeyboardInterrupt())
    rc = run_watch_loop(
        session, dispatcher, safety_sweep_seconds=None, once=False
    )
    assert rc == 0


def test_does_not_import_pywatchman_directly() -> None:
    """AC-30: helper module must not import pywatchman."""
    import autofix.cli._watch_loop as mod
    src = mod.__file__
    assert src is not None
    with open(src, encoding="utf-8") as f:
        text = f.read()
    assert "import pywatchman" not in text
    assert "from pywatchman" not in text
