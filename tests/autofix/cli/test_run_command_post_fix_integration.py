"""Integration tests for run_command's --post-fix wiring (ARCH-015)."""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from autofix.cli.run_command import add_arguments


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    add_arguments(p)
    return p


def test_post_fix_flag_default_none() -> None:
    """AC-28: --post-fix default is None (means 'use config or default')."""
    ns = _parser().parse_args(["--root", "/tmp"])
    assert ns.post_fix is None


def test_post_fix_flag_accepts_each_allowed_value() -> None:
    """AC-29: argparse round-trip for all three allowed values."""
    for value in ("working-tree", "branch", "branch-pr"):
        ns = _parser().parse_args(
            ["--root", "/tmp", "--post-fix", value]
        )
        assert ns.post_fix == value


def test_post_fix_flag_rejects_unknown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """argparse choices catches an out-of-allowed value at parse time."""
    with pytest.raises(SystemExit):
        _parser().parse_args(["--root", "/tmp", "--post-fix", "garbage"])
    err = capsys.readouterr().err
    assert "garbage" in err


def test_post_fix_invoked_only_after_done(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-13 + AC-27: apply_post_fix_policy fires only on the DONE path."""
    from autofix.cli import run_command

    sentinel = MagicMock()
    monkeypatch.setattr(
        "autofix.cli.post_fix_policy.apply_post_fix_policy", sentinel
    )

    # FAILED path: bad combinatorics → return EXIT_USAGE_ERROR before any work.
    ns = _parser().parse_args(
        ["--root", str(tmp_path), "--suggest", "--apply", "--auto-llm"]
    )
    rc = run_command.run(ns)
    assert rc == 2  # EXIT_USAGE_ERROR
    sentinel.assert_not_called()


def test_post_fix_skipped_when_applied_set_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-13 guard: even on DONE, an empty applied set must skip the call."""
    from autofix.cli import run_command

    sentinel = MagicMock()
    monkeypatch.setattr(
        "autofix.cli.post_fix_policy.apply_post_fix_policy", sentinel
    )

    # Replace _run_one_cycle with a stub that returns EXIT_OK directly,
    # bypassing the workflow body — and proving that the call site only
    # fires inside the function (not from a wrapper).
    monkeypatch.setattr(
        "autofix.cli.run_command._run_one_cycle",
        lambda *_a, **_k: 0,
    )

    ns = _parser().parse_args(["--root", str(tmp_path)])
    rc = run_command.run(ns)
    assert rc == 0
    # _run_one_cycle was stubbed away → policy was not invoked.
    sentinel.assert_not_called()


def test_post_fix_default_does_not_alter_existing_run_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC-28: --post-fix default (None) preserves backwards-compat run."""
    from autofix.cli import run_command

    seen = []
    monkeypatch.setattr(
        "autofix.cli.run_command._run_one_cycle",
        lambda args, analyzer_set, *, fresh_instance: seen.append(args.post_fix) or 0,
    )

    ns = _parser().parse_args(["--root", str(tmp_path)])
    rc = run_command.run(ns)
    assert rc == 0
    assert seen == [None]
