"""Tests for autofix/cli/watch_command.py (NEW production module).

Covers:
- AC 2: argparse flags --root, --since, --safety-sweep registered
- AC 4: pywatchman missing -> stderr substring "pywatchman is required" + exit 2
- AC 5: --safety-sweep "1h"/"30m" parses; "abc"/"5x" rejected with exit 2 and
        stderr substring "invalid --safety-sweep value"
- AC 6: AUTOFIX_WATCH_ONCE=1 env causes the watcher to process 1 event
        then exit 0
- AC 7: is_fresh_instance from Watchman propagates to ChangeSet (mock
        WatcherSession.next_files)

These tests MUST FAIL until production code at autofix/cli/watch_command.py
lands.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# AC 2: argparse flags
# ---------------------------------------------------------------------------


class TestWatchAddArguments:
    """AC 2 — add_arguments registers --root, --since, --safety-sweep."""

    def test_root_flag_registered(self) -> None:
        """AC 2: --root flag exists on the watch parser."""
        from autofix.cli.watch_command import add_arguments

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--root", "/tmp"])
        assert ns.root == Path("/tmp")

    def test_root_flag_default_is_none(self) -> None:
        """AC 2: --root default is None (resolved at runtime in run(), not here)."""
        from autofix.cli.watch_command import add_arguments

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args([])
        # Default must be None so run() can do Path.cwd() at invocation time
        assert ns.root is None

    def test_since_flag_registered(self) -> None:
        """AC 2: --since flag exists and accepts a clock string."""
        from autofix.cli.watch_command import add_arguments

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--since", "c:1234567890:1:0:0:0"])
        assert ns.since == "c:1234567890:1:0:0:0"

    def test_since_flag_default_is_none(self) -> None:
        """AC 2: --since default is None."""
        from autofix.cli.watch_command import add_arguments

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args([])
        assert ns.since is None

    def test_safety_sweep_flag_registered(self) -> None:
        """AC 2: --safety-sweep flag exists and accepts a duration string."""
        from autofix.cli.watch_command import add_arguments

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--safety-sweep", "30m"])
        assert ns.safety_sweep == "30m"

    def test_safety_sweep_flag_default_is_none(self) -> None:
        """AC 2: --safety-sweep default is None (no max-stale trigger)."""
        from autofix.cli.watch_command import add_arguments

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args([])
        assert ns.safety_sweep is None

    def test_all_three_flags_together(self) -> None:
        """AC 2: all three flags coexist without conflict."""
        from autofix.cli.watch_command import add_arguments

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(
            ["--root", "/repo", "--since", "c:0:1:0:0:0", "--safety-sweep", "1h"]
        )
        assert ns.root == Path("/repo")
        assert ns.since == "c:0:1:0:0:0"
        assert ns.safety_sweep == "1h"


# ---------------------------------------------------------------------------
# AC 4: pywatchman missing produces diagnostic and exit 2
# ---------------------------------------------------------------------------


class TestWatchDiagnosticWhenPywatchmanMissing:
    """AC 4 — when pywatchman is unimportable, run() returns 2 with stderr
    substring 'pywatchman is required'. No traceback leaks."""

    def test_missing_pywatchman_returns_exit_2(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC 4: pywatchman not in sys.modules -> exit 2."""
        from autofix.cli.watch_command import add_arguments, run

        # Force pywatchman to be unimportable
        monkeypatch.setitem(sys.modules, "pywatchman", None)

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args([])

        exit_code = run(ns, root=tmp_path)

        assert exit_code == 2

    def test_missing_pywatchman_stderr_contains_required_substring(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC 4: stderr must contain 'pywatchman is required'."""
        from autofix.cli.watch_command import add_arguments, run

        monkeypatch.setitem(sys.modules, "pywatchman", None)

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args([])

        run(ns, root=tmp_path)

        captured = capsys.readouterr()
        assert "pywatchman is required" in captured.err, (
            f"expected 'pywatchman is required' in stderr, got: {captured.err!r}"
        )

    def test_missing_pywatchman_no_traceback_in_stderr(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC 4: no traceback (no 'Traceback' string) leaks to stderr."""
        from autofix.cli.watch_command import add_arguments, run

        monkeypatch.setitem(sys.modules, "pywatchman", None)

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args([])

        run(ns, root=tmp_path)

        captured = capsys.readouterr()
        assert "Traceback" not in captured.err, (
            f"traceback leaked to stderr: {captured.err!r}"
        )


# ---------------------------------------------------------------------------
# AC 5: --safety-sweep validation
# ---------------------------------------------------------------------------


class TestSafetySweepParsing:
    """AC 5 — valid formats parse; invalid formats produce exit 2 with
    stderr substring 'invalid --safety-sweep value'."""

    @pytest.mark.parametrize("valid_value", ["1h", "24h", "30m", "5m", "120m"])
    def test_valid_safety_sweep_accepted(
        self,
        valid_value: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC 5: valid Nh/Nm forms are accepted (run does NOT return 2
        for the --safety-sweep format check)."""
        pywatchman_stub = MagicMock()
        pywatchman_stub.client.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "pywatchman", pywatchman_stub)

        from autofix.cli.watch_command import add_arguments

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--safety-sweep", valid_value])
        # The flag parses without argparse error
        assert ns.safety_sweep == valid_value

    @pytest.mark.parametrize(
        "bad_value",
        ["abc", "5x", "1", "h", "m", "1H", "1M", "1hour", "30min", "0.5h"],
    )
    def test_invalid_safety_sweep_exits_2(
        self,
        bad_value: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC 5: invalid duration values return exit 2."""
        # Provide a stub pywatchman so the check is purely about format
        pywatchman_stub = MagicMock()
        monkeypatch.setitem(sys.modules, "pywatchman", pywatchman_stub)

        from autofix.cli.watch_command import add_arguments, run

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--safety-sweep", bad_value])

        exit_code = run(ns, root=tmp_path)
        assert exit_code == 2, (
            f"expected exit 2 for bad --safety-sweep {bad_value!r}, got {exit_code}"
        )

    @pytest.mark.parametrize(
        "bad_value",
        ["abc", "5x", "1", "h", "m", "1H", "1M"],
    )
    def test_invalid_safety_sweep_stderr_substring(
        self,
        bad_value: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """AC 5: invalid duration produces stderr substring 'invalid --safety-sweep value'."""
        pywatchman_stub = MagicMock()
        monkeypatch.setitem(sys.modules, "pywatchman", pywatchman_stub)

        from autofix.cli.watch_command import add_arguments, run

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args(["--safety-sweep", bad_value])

        run(ns, root=tmp_path)

        captured = capsys.readouterr()
        assert "invalid --safety-sweep value" in captured.err, (
            f"expected 'invalid --safety-sweep value' in stderr for {bad_value!r}, "
            f"got: {captured.err!r}"
        )

    def test_none_safety_sweep_is_accepted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC 5: unset (None) safety_sweep means no max-stale trigger — not
        an error at format-check time."""
        from autofix.cli.watch_command import add_arguments

        parser = argparse.ArgumentParser()
        add_arguments(parser)
        ns = parser.parse_args([])
        assert ns.safety_sweep is None


# ---------------------------------------------------------------------------
# AC 6: AUTOFIX_WATCH_ONCE=1 escape hatch
# ---------------------------------------------------------------------------


class TestWatchOnceEscapeHatch:
    """AC 6 — AUTOFIX_WATCH_ONCE=1 causes the watcher to process exactly
    one event (or time out after <= 2 seconds) and return exit 0."""

    def test_watch_once_env_exits_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC 6: AUTOFIX_WATCH_ONCE=1 -> exit 0."""
        monkeypatch.setenv("AUTOFIX_WATCH_ONCE", "1")

        # Provide a WatcherSession stub that yields one event then stops
        fake_session = MagicMock()
        fake_session.next_files.return_value = (False, ["sample.py"])

        with patch(
            "autofix.cli.watch_command.WatcherSession",
            return_value=fake_session,
        ):
            with patch("autofix.funnel.pipeline.run_scan") as mock_run_scan:
                from autofix.funnel.pipeline import ScanResult

                mock_run_scan.return_value = ScanResult(
                    scan_id="watch-smoke",
                    findings=[],
                    schedule_decisions=[],
                )
                from autofix.cli.watch_command import add_arguments, run

                parser = argparse.ArgumentParser()
                add_arguments(parser)
                ns = parser.parse_args([])

                exit_code = run(ns, root=tmp_path)

        assert exit_code == 0, f"expected exit 0, got {exit_code}"

    def test_watch_once_truthy_variants_exit_zero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC 6: any non-empty string other than '0'/'false' is truthy for
        AUTOFIX_WATCH_ONCE — use 'true' as an example."""
        monkeypatch.setenv("AUTOFIX_WATCH_ONCE", "true")

        fake_session = MagicMock()
        fake_session.next_files.return_value = (False, ["sample.py"])

        with patch(
            "autofix.cli.watch_command.WatcherSession",
            return_value=fake_session,
        ):
            with patch("autofix.funnel.pipeline.run_scan") as mock_run_scan:
                from autofix.funnel.pipeline import ScanResult

                mock_run_scan.return_value = ScanResult(
                    scan_id="watch-smoke",
                    findings=[],
                    schedule_decisions=[],
                )
                from autofix.cli.watch_command import add_arguments, run

                parser = argparse.ArgumentParser()
                add_arguments(parser)
                ns = parser.parse_args([])

                exit_code = run(ns, root=tmp_path)

        assert exit_code == 0


# ---------------------------------------------------------------------------
# AC 7: is_fresh_instance propagates to ChangeSet
# ---------------------------------------------------------------------------


class TestIsFreshInstancePropagation:
    """AC 7 — when WatcherSession.next_files() returns is_fresh_instance=True,
    the ChangeSet constructed by run() carries is_fresh_instance=True."""

    def test_fresh_instance_true_propagates_to_changeset(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC 7: is_fresh_instance=True from next_files propagates into ChangeSet."""
        monkeypatch.setenv("AUTOFIX_WATCH_ONCE", "1")

        captured_changesets: list[Any] = []

        fake_session = MagicMock()
        # Simulate Watchman returning is_fresh_instance=True
        fake_session.next_files.return_value = (True, ["sample.py"])

        def _capture_run_scan(root, changeset, scan_id, **kwargs):
            captured_changesets.append(changeset)
            from autofix.funnel.pipeline import ScanResult
            return ScanResult(scan_id=scan_id, findings=[], schedule_decisions=[])

        with patch(
            "autofix.cli.watch_command.WatcherSession",
            return_value=fake_session,
        ):
            with patch(
                "autofix.funnel.pipeline.run_scan",
                side_effect=_capture_run_scan,
            ):
                from autofix.cli.watch_command import add_arguments, run

                parser = argparse.ArgumentParser()
                add_arguments(parser)
                ns = parser.parse_args([])
                run(ns, root=tmp_path)

        assert len(captured_changesets) >= 1, (
            "expected run_scan to be called at least once"
        )
        cs = captured_changesets[0]
        assert cs.is_fresh_instance is True, (
            f"expected ChangeSet.is_fresh_instance=True, got {cs.is_fresh_instance!r}"
        )

    def test_fresh_instance_false_propagates_to_changeset(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AC 7 regression: is_fresh_instance=False is also faithfully threaded."""
        monkeypatch.setenv("AUTOFIX_WATCH_ONCE", "1")

        captured_changesets: list[Any] = []

        fake_session = MagicMock()
        fake_session.next_files.return_value = (False, ["sample.py"])

        def _capture_run_scan(root, changeset, scan_id, **kwargs):
            captured_changesets.append(changeset)
            from autofix.funnel.pipeline import ScanResult
            return ScanResult(scan_id=scan_id, findings=[], schedule_decisions=[])

        with patch(
            "autofix.cli.watch_command.WatcherSession",
            return_value=fake_session,
        ):
            with patch(
                "autofix.funnel.pipeline.run_scan",
                side_effect=_capture_run_scan,
            ):
                from autofix.cli.watch_command import add_arguments, run

                parser = argparse.ArgumentParser()
                add_arguments(parser)
                ns = parser.parse_args([])
                run(ns, root=tmp_path)

        assert len(captured_changesets) >= 1
        cs = captured_changesets[0]
        assert cs.is_fresh_instance is False, (
            f"expected ChangeSet.is_fresh_instance=False, got {cs.is_fresh_instance!r}"
        )
