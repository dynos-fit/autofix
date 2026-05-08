"""Argparse + flag combinatorics for `autofix run --watch` (ARCH-014)."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from autofix.cli.run_command import add_arguments


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    add_arguments(p)
    return p


def test_watch_flag_registered() -> None:
    ns = _parser().parse_args(["--root", "/tmp", "--watch"])
    assert ns.watch is True


def test_watch_flag_default_false() -> None:
    ns = _parser().parse_args(["--root", "/tmp"])
    assert ns.watch is False


def test_since_flag_registered() -> None:
    ns = _parser().parse_args(
        ["--root", "/tmp", "--watch", "--since", "c:1234567890:1:0:0:0"]
    )
    assert ns.since == "c:1234567890:1:0:0:0"


def test_since_flag_default_none() -> None:
    ns = _parser().parse_args(["--root", "/tmp"])
    assert ns.since is None


def test_safety_sweep_flag_registered() -> None:
    ns = _parser().parse_args(
        ["--root", "/tmp", "--watch", "--safety-sweep", "30m"]
    )
    assert ns.safety_sweep == "30m"


def test_safety_sweep_flag_default_none() -> None:
    ns = _parser().parse_args(["--root", "/tmp"])
    assert ns.safety_sweep is None


def test_combinatorics_validation_runs_before_watch_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--suggest + --auto-llm must error out before any WatcherSession build."""
    from autofix.cli import run_command

    # Sentinel: WatcherSession should NEVER be constructed in this flow.
    sentinel = object()

    def _watcher_session_unreachable(*_a, **_k):  # pragma: no cover
        raise AssertionError("WatcherSession built before combinatorics validation")

    monkeypatch.setattr(
        "autofix.cli.watch_command.WatcherSession",
        _watcher_session_unreachable,
    )

    ns = _parser().parse_args(
        [
            "--root", "/tmp",
            "--watch",
            "--suggest",
            "--apply",
            "--auto-llm",
        ]
    )
    rc = run_command.run(ns)
    assert rc == 2  # EXIT_USAGE_ERROR


def test_bad_safety_sweep_with_watch_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from autofix.cli import run_command

    ns = _parser().parse_args(
        ["--root", "/tmp", "--watch", "--safety-sweep", "abc"]
    )
    rc = run_command.run(ns)
    assert rc == 2
    err = capsys.readouterr().err
    assert "invalid --safety-sweep value" in err


def test_pywatchman_missing_with_watch_exits_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from autofix.cli import run_command

    # Simulate pywatchman not installed: WatcherSession.__init__ raises.
    monkeypatch.setattr(
        "autofix.cli.watch_command.WatcherSession",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError(
                "pywatchman is required for `autofix watch`. "
                "Install with `pip install autofix-standalone[watch]` "
                "(also requires the `watchman` daemon binary)."
            )
        ),
    )

    ns = _parser().parse_args(["--root", "/tmp", "--watch"])
    rc = run_command.run(ns)
    assert rc == 2
    err = capsys.readouterr().err
    assert "pywatchman is required" in err


def test_non_watch_path_does_not_call_watch_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """AC-25: --watch=False must take the existing single-shot path
    (run_watch_loop is NEVER called)."""
    from autofix.cli import run_command

    sentinel_called = {"flag": False}

    def _sentinel(*_a, **_k) -> int:  # pragma: no cover
        sentinel_called["flag"] = True
        return 0

    monkeypatch.setattr(
        "autofix.cli._watch_loop.run_watch_loop", _sentinel
    )
    # Stub _run_one_cycle to a no-op so we don't drive the real workflow.
    monkeypatch.setattr(
        "autofix.cli.run_command._run_one_cycle",
        lambda *_a, **_k: 0,
    )

    ns = _parser().parse_args(["--root", str(tmp_path)])
    rc = run_command.run(ns)
    assert rc == 0
    assert sentinel_called["flag"] is False
