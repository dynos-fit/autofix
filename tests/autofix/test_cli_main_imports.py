"""Tests for autofix/cli/main.py import safety and subcommand registration.

Covers:
- AC 13 + C-04: import autofix.cli.main succeeds with pywatchman patched out;
                import autofix.cli.watch_command does NOT trigger pywatchman
                load at module-import time.
- AC 12 + C-05: autofix --help exposes exactly 5 subcommands in order:
                scan, replay, export-sarif, watch, policy.

These tests MUST FAIL until production code in autofix/cli/main.py
and autofix/cli/watch_command.py lands.
"""
# seg-6 validated under task-20260506-002 (clean-slate-cli-cutover).

from __future__ import annotations

import importlib
import sys

import pytest


# ---------------------------------------------------------------------------
# AC 13 + C-04: deferred pywatchman import discipline
# ---------------------------------------------------------------------------


class TestDeferredPywatchmanImport:
    """AC 13 + C-04 — pywatchman must NOT be loaded at module-import time by
    either main.py or watch_command.py."""

    def test_import_main_succeeds_when_pywatchman_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC 13: import autofix.cli.main succeeds even when pywatchman is
        blocked from the module namespace."""
        # Block pywatchman at the sys.modules level so any top-level
        # `import pywatchman` would raise ImportError
        monkeypatch.setitem(sys.modules, "pywatchman", None)

        # Re-import the module to exercise the import-time path.
        # If watch_command loads pywatchman at module level, this will raise.
        import autofix.cli.main  # noqa: F401 — import for side-effect check

        # If we get here without ImportError, the test passes
        assert "autofix.cli.main" in sys.modules

    def test_pywatchman_not_in_sys_modules_after_main_import(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC 13 + C-04: after importing main.py, pywatchman is still absent
        from sys.modules (deferred-import discipline holds)."""
        # Remove pywatchman entirely so it is genuinely absent
        monkeypatch.delitem(sys.modules, "pywatchman", raising=False)
        # Also set it to None so an attempted import raises ImportError
        monkeypatch.setitem(sys.modules, "pywatchman", None)

        import autofix.cli.main  # noqa: F401

        # The None sentinel is what we set; verify it wasn't replaced by
        # a real module object (which would mean the deferred-import
        # invariant was violated at module load time).
        val = sys.modules.get("pywatchman")
        # None means "blocked" (our sentinel), not a real module
        assert val is None, (
            f"pywatchman appears to have been loaded at main.py import time: {val!r}"
        )

    def test_import_watch_command_does_not_trigger_pywatchman_load(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """C-04: import autofix.cli.watch_command does NOT load pywatchman at
        module-import time."""
        # Remove any cached pywatchman first
        monkeypatch.delitem(sys.modules, "pywatchman", raising=False)
        # Do NOT set it to None — we want to verify it never gets imported,
        # not that the import fails. We check sys.modules after import.

        # Clear any cached watch_command to force a fresh import
        monkeypatch.delitem(sys.modules, "autofix.cli.watch_command", raising=False)

        import autofix.cli.watch_command  # noqa: F401

        assert "pywatchman" not in sys.modules, (
            "pywatchman was loaded at autofix.cli.watch_command module-import "
            "time; it must be deferred to WatcherSession.__init__"
        )

    def test_import_watch_command_exports_add_arguments_and_run(self) -> None:
        """AC 1: watch_command exports add_arguments and run callables."""
        from autofix.cli.watch_command import add_arguments, run  # noqa: F401

        import autofix.cli.watch_command as wc

        assert callable(wc.add_arguments), "add_arguments must be callable"
        assert callable(wc.run), "run must be callable"


# ---------------------------------------------------------------------------
# AC 12 + C-05: exactly 5 subcommands in specified order
# ---------------------------------------------------------------------------


class TestMainSubcommandRegistration:
    """AC 12 + C-05 — _build_parser() must register exactly 5 subcommands in
    the order: scan, replay, export-sarif, watch, policy."""

    def test_exactly_five_subcommands(self) -> None:
        """AC 12: parser exposes exactly 5 subcommands."""
        from autofix.cli.main import _build_parser

        parser = _build_parser()
        # Walk the parser's subactions to find the subcommand names
        subcommand_names = _get_subcommand_names(parser)
        assert len(subcommand_names) == 5, (
            f"expected 5 subcommands, got {len(subcommand_names)}: {subcommand_names}"
        )

    def test_subcommands_order_is_correct(self) -> None:
        """AC 12 + C-05: subcommands appear in order: scan, replay, export-sarif,
        watch, policy."""
        from autofix.cli.main import _build_parser

        parser = _build_parser()
        subcommand_names = _get_subcommand_names(parser)

        expected_order = ["scan", "replay", "export-sarif", "watch", "policy"]
        assert subcommand_names == expected_order, (
            f"expected subcommand order {expected_order}, got {subcommand_names}"
        )

    def test_scan_subcommand_is_registered(self) -> None:
        """AC 12: scan is present as a subcommand."""
        from autofix.cli.main import _build_parser

        parser = _build_parser()
        ns = parser.parse_args(["scan", "--root", "."])
        assert ns.subcommand == "scan"

    def test_replay_subcommand_is_registered(self) -> None:
        """AC 12: replay is present as a subcommand."""
        from autofix.cli.main import _build_parser

        parser = _build_parser()
        ns = parser.parse_args(["replay", "--scan-id", "x"])
        assert ns.subcommand == "replay"

    def test_export_sarif_subcommand_is_registered(self) -> None:
        """AC 12: export-sarif is present as a subcommand."""
        from autofix.cli.main import _build_parser

        parser = _build_parser()
        ns = parser.parse_args(["export-sarif", "--scan-id", "x"])
        assert ns.subcommand == "export-sarif"

    def test_watch_subcommand_is_registered(self) -> None:
        """AC 12: watch is present as a subcommand."""
        from autofix.cli.main import _build_parser

        parser = _build_parser()
        ns = parser.parse_args(["watch"])
        assert ns.subcommand == "watch"

    def test_policy_subcommand_is_registered(self) -> None:
        """AC 12: policy is present as a subcommand (requires --show or --validate)."""
        from autofix.cli.main import _build_parser

        parser = _build_parser()
        ns = parser.parse_args(["policy", "--show"])
        assert ns.subcommand == "policy"

    def test_existing_subcommands_preserved(self) -> None:
        """AC 12: the existing scan/replay/export-sarif registrations are
        preserved — their _runner hooks still point to the right modules."""
        from autofix.cli import export_sarif_command, replay_command, scan_command
        from autofix.cli.main import _build_parser

        parser = _build_parser()

        ns_scan = parser.parse_args(["scan", "--root", "."])
        assert getattr(ns_scan, "_runner", None) is scan_command.run

        ns_replay = parser.parse_args(["replay", "--scan-id", "x"])
        assert getattr(ns_replay, "_runner", None) is replay_command.run

        ns_sarif = parser.parse_args(["export-sarif", "--scan-id", "x"])
        assert getattr(ns_sarif, "_runner", None) is export_sarif_command.run


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _get_subcommand_names(parser) -> list[str]:
    """Extract the ordered list of subcommand names from an ArgumentParser."""
    # argparse stores subparsers as a special action with _name_parser_map
    for action in parser._actions:
        if hasattr(action, "_name_parser_map"):
            # _name_parser_map preserves insertion order in Python 3.7+
            return list(action._name_parser_map.keys())
    return []
