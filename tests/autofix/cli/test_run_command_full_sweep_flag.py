"""Tests for --full-sweep flag default + warning + scope plumbing.

Default scope on ``autofix run`` is now diff (HEAD~1..HEAD), not
the whole repo. ``--full-sweep`` widens it explicitly. Combining
``--full-sweep`` with any LLM analyzer prints a stderr warning so
the cost is visible.
"""
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


def test_full_sweep_flag_default_false() -> None:
    """Default scope is diff — operators must opt into the wide scan."""
    ns = _parser().parse_args(["--root", "/tmp"])
    assert ns.full_sweep is False


def test_full_sweep_flag_parses_to_true() -> None:
    ns = _parser().parse_args(["--root", "/tmp", "--full-sweep"])
    assert ns.full_sweep is True


def test_run_passes_diff_scope_to_scan_core_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Default invocation: _run_scan_core must be called with full_sweep=False."""
    from autofix.cli import run_command

    captured = {}

    def _fake_scan_core(*, root, full_sweep, analyzer_set, scan_id, fresh_instance, quiet):
        captured["full_sweep"] = full_sweep
        from autofix.cli.scan_command import ScanCoreResult
        return ScanCoreResult(
            exit_code=0, findings=[], sarif_path=None,
            scan_id="test", watcher_confidence="diff-head1",
        )

    monkeypatch.setattr(
        "autofix.cli.run_command._run_scan_core", _fake_scan_core
    )

    ns = _parser().parse_args(["--root", str(tmp_path)])
    rc = run_command.run(ns)
    assert rc == 3  # HUMAN_REVIEW (no --apply)
    assert captured["full_sweep"] is False, (
        "Default scope must be diff — full_sweep should be False"
    )


def test_run_passes_full_sweep_when_flag_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from autofix.cli import run_command

    captured = {}

    def _fake_scan_core(*, root, full_sweep, analyzer_set, scan_id, fresh_instance, quiet):
        captured["full_sweep"] = full_sweep
        from autofix.cli.scan_command import ScanCoreResult
        return ScanCoreResult(
            exit_code=0, findings=[], sarif_path=None,
            scan_id="test", watcher_confidence="full-sweep",
        )

    monkeypatch.setattr(
        "autofix.cli.run_command._run_scan_core", _fake_scan_core
    )

    ns = _parser().parse_args(["--root", str(tmp_path), "--full-sweep"])
    run_command.run(ns)
    assert captured["full_sweep"] is True


def test_full_sweep_with_llm_analyzer_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The expensive combo must surface a stderr warning."""
    from autofix.cli import run_command

    monkeypatch.setattr(
        "autofix.cli.run_command._run_scan_core",
        lambda *_a, **_k: __import__("autofix.cli.scan_command", fromlist=["ScanCoreResult"]).ScanCoreResult(
            exit_code=0, findings=[], sarif_path=None,
            scan_id="t", watcher_confidence="full-sweep",
        ),
    )

    ns = _parser().parse_args([
        "--root", str(tmp_path),
        "--full-sweep",
        "--apply",
        "--auto-llm",  # auto-expands to 4 LLM analyzers
    ])
    run_command.run(ns)
    err = capsys.readouterr().err
    assert "warning" in err.lower()
    assert "--full-sweep" in err
    assert "LLM analyzer" in err
    assert "Drop --full-sweep" in err


def test_full_sweep_without_llm_no_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--full-sweep alone (no LLM analyzer) doesn't warn — cheap-only
    is fast even repo-wide."""
    from autofix.cli import run_command

    monkeypatch.setattr(
        "autofix.cli.run_command._run_scan_core",
        lambda *_a, **_k: __import__("autofix.cli.scan_command", fromlist=["ScanCoreResult"]).ScanCoreResult(
            exit_code=0, findings=[], sarif_path=None,
            scan_id="t", watcher_confidence="full-sweep",
        ),
    )

    ns = _parser().parse_args([
        "--root", str(tmp_path),
        "--full-sweep",
        "--analyzers", "cheap",
    ])
    run_command.run(ns)
    err = capsys.readouterr().err
    assert "warning" not in err.lower()


def test_full_sweep_warning_suppressed_under_quiet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from autofix.cli import run_command

    monkeypatch.setattr(
        "autofix.cli.run_command._run_scan_core",
        lambda *_a, **_k: __import__("autofix.cli.scan_command", fromlist=["ScanCoreResult"]).ScanCoreResult(
            exit_code=0, findings=[], sarif_path=None,
            scan_id="t", watcher_confidence="full-sweep",
        ),
    )

    ns = _parser().parse_args([
        "--root", str(tmp_path),
        "--full-sweep",
        "--apply",
        "--auto-llm",
        "--quiet",
    ])
    run_command.run(ns)
    err = capsys.readouterr().err
    assert "warning" not in err.lower()


def test_watch_dispatcher_overrides_full_sweep_via_fresh_instance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Even with --full-sweep=False, a Watchman fresh-instance signal
    must trigger a full sweep on that cycle (cold-start semantics)."""
    from autofix.cli import run_command

    captured = []

    def _fake_scan_core(*, root, full_sweep, analyzer_set, scan_id, fresh_instance, quiet):
        captured.append({"full_sweep": full_sweep, "fresh_instance": fresh_instance})
        from autofix.cli.scan_command import ScanCoreResult
        return ScanCoreResult(
            exit_code=0, findings=[], sarif_path=None,
            scan_id="t", watcher_confidence="full-sweep",
        )

    monkeypatch.setattr(
        "autofix.cli.run_command._run_scan_core", _fake_scan_core
    )

    # Drive _run_one_cycle directly with fresh_instance=True.
    ns = _parser().parse_args(["--root", str(tmp_path)])
    run_command._run_one_cycle(ns, None, fresh_instance=True)
    assert captured == [
        {"full_sweep": True, "fresh_instance": True},
    ], "fresh_instance must imply full_sweep regardless of CLI flag"
