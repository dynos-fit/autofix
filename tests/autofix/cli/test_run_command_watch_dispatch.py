"""Dispatcher closure + per-cycle behavior for `autofix run --watch` (ARCH-014)."""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from autofix.cli.run_command import add_arguments
from autofix.events.schema import ChangeSet


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    add_arguments(p)
    return p


def test_watch_path_invokes_dispatcher_with_changeset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC-9.d: the watch path passes a dispatcher closure to
    run_watch_loop. We capture it and assert it forwards a ChangeSet
    into _run_one_cycle."""
    from autofix.cli import run_command

    # Stub WatcherSession + subscribe so no real watchman is contacted.
    fake_session = MagicMock()
    fake_session.subscribe.return_value = None
    fake_session.close.return_value = None
    monkeypatch.setattr(
        "autofix.cli.watch_command.WatcherSession",
        lambda *_a, **_k: fake_session,
    )

    captured = {}

    def _capture(session, dispatcher, *, safety_sweep_seconds, once) -> int:
        captured["dispatcher"] = dispatcher
        captured["safety_sweep_seconds"] = safety_sweep_seconds
        captured["once"] = once
        # Drive the dispatcher once with a synthesized ChangeSet.
        cs = ChangeSet(
            paths=("foo.py",),
            watcher_confidence="watch",
            is_fresh_instance=False,
        )
        dispatcher(cs)
        return 0

    monkeypatch.setattr(
        "autofix.cli._watch_loop.run_watch_loop", _capture
    )

    cycles_seen = []

    def _stub_cycle(args, analyzer_set, *, fresh_instance):
        cycles_seen.append((args.apply, fresh_instance, analyzer_set))
        return 0

    monkeypatch.setattr(
        "autofix.cli.run_command._run_one_cycle", _stub_cycle
    )

    ns = _parser().parse_args(
        [
            "--root", str(tmp_path),
            "--watch",
            "--apply",
            "--safety-sweep", "1h",
        ]
    )
    rc = run_command.run(ns)

    assert rc == 0
    assert captured["safety_sweep_seconds"] == 3600
    assert callable(captured["dispatcher"])
    assert cycles_seen == [(True, False, None)]


def test_watch_dispatcher_propagates_fresh_instance_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC-9.d + AC-28: fresh_instance=True flows from changeset to
    _run_one_cycle so the per-cycle scan flags itself as full-sweep."""
    from autofix.cli import run_command

    fake_session = MagicMock()
    monkeypatch.setattr(
        "autofix.cli.watch_command.WatcherSession",
        lambda *_a, **_k: fake_session,
    )

    cycles_seen = []

    def _drive_dispatcher(session, dispatcher, *, safety_sweep_seconds, once):
        cs = ChangeSet(
            paths=(),
            watcher_confidence="full-sweep",
            is_fresh_instance=True,
        )
        dispatcher(cs)
        return 0

    monkeypatch.setattr(
        "autofix.cli._watch_loop.run_watch_loop", _drive_dispatcher
    )
    monkeypatch.setattr(
        "autofix.cli.run_command._run_one_cycle",
        lambda args, analyzer_set, *, fresh_instance: cycles_seen.append(fresh_instance) or 0,
    )

    ns = _parser().parse_args(["--root", str(tmp_path), "--watch"])
    rc = run_command.run(ns)
    assert rc == 0
    assert cycles_seen == [True]


def test_watch_emits_cycle_progress_lines(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-12: one stderr [CYCLE n] line per dispatched cycle when not --quiet."""
    from autofix.cli import run_command

    monkeypatch.setattr(
        "autofix.cli.watch_command.WatcherSession",
        lambda *_a, **_k: MagicMock(),
    )

    def _drive_two_cycles(session, dispatcher, *, safety_sweep_seconds, once):
        for _ in range(2):
            dispatcher(
                ChangeSet(paths=(), watcher_confidence="watch", is_fresh_instance=False)
            )
        return 0

    monkeypatch.setattr(
        "autofix.cli._watch_loop.run_watch_loop", _drive_two_cycles
    )
    monkeypatch.setattr(
        "autofix.cli.run_command._run_one_cycle",
        lambda *_a, **_k: 0,
    )

    ns = _parser().parse_args(["--root", str(tmp_path), "--watch"])
    rc = run_command.run(ns)
    assert rc == 0
    err = capsys.readouterr().err
    assert "[CYCLE 1]" in err
    assert "[CYCLE 2]" in err


def test_watch_quiet_suppresses_cycle_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC-12 negative: --quiet suppresses [CYCLE n] lines."""
    from autofix.cli import run_command

    monkeypatch.setattr(
        "autofix.cli.watch_command.WatcherSession",
        lambda *_a, **_k: MagicMock(),
    )
    monkeypatch.setattr(
        "autofix.cli._watch_loop.run_watch_loop",
        lambda session, dispatcher, *, safety_sweep_seconds, once: (
            dispatcher(ChangeSet(paths=(), watcher_confidence="watch", is_fresh_instance=False))
            or 0
        ),
    )
    monkeypatch.setattr(
        "autofix.cli.run_command._run_one_cycle", lambda *_a, **_k: 0
    )

    ns = _parser().parse_args(
        ["--root", str(tmp_path), "--watch", "--quiet"]
    )
    rc = run_command.run(ns)
    assert rc == 0
    err = capsys.readouterr().err
    assert "[CYCLE" not in err
