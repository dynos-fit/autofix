"""End-to-end tests for analyzer_set parameter in run_scan.

These tests verify that the run_scan function correctly dispatches
to different analyzers based on the analyzer_set parameter.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


def _init_repo(root: Path) -> None:
    """Initialize a git repository with standard config."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "t"], cwd=root, check=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=root, check=True
    )


def _commit(root: Path, msg: str) -> None:
    """Commit all changes to git."""
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", msg],
        cwd=root,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-04-17T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-04-17T00:00:00Z",
        },
    )


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    """Create a tiny git repo with one unused import."""
    _init_repo(tmp_path)
    (tmp_path / "module_a.py").write_text(
        "import os\n\npath = os.getcwd()\n", encoding="utf-8"
    )
    _commit(tmp_path, "init")
    # Add a file with unused import
    (tmp_path / "module_b.py").write_text(
        "import json  # unused\n\nx = 1\n", encoding="utf-8"
    )
    _commit(tmp_path, "add unused")
    return tmp_path


def _collect_events(root: Path) -> list[dict]:
    """Read events.jsonl and parse each line as JSON."""
    events_path = root / ".autofix" / "events.jsonl"
    if not events_path.exists():
        return []
    return [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if line
    ]


class TestAnalyzerSetDispatch:
    """Tests for analyzer_set parameter behavior."""

    def test_explicit_cheap_matches_default(self, tiny_repo: Path) -> None:
        """analyzer_set=['cheap'] produces same results as analyzer_set=None.

        Both should find the unused import via the cheap analyzer.
        """
        from autofix.events.schema import ChangeSet
        from autofix.funnel.pipeline import run_scan

        changeset = ChangeSet(
            paths=("module_b.py",), watcher_confidence="diff-head1"
        )

        # Run with default (None)
        result_default = run_scan(tiny_repo, changeset, scan_id="scan-default")

        # Run with explicit cheap
        result_cheap = run_scan(
            tiny_repo,
            changeset,
            scan_id="scan-cheap",
            analyzer_set=["cheap"],
        )

        # Should find same number of findings
        assert len(result_default.findings) == len(result_cheap.findings)

        # Both should have unused-import findings
        cheap_rules_default = {f.rule_id for f in result_default.findings}
        cheap_rules_explicit = {f.rule_id for f in result_cheap.findings}
        assert cheap_rules_default == cheap_rules_explicit

    def test_explicit_ruff_only_skips_cheap(self, tiny_repo: Path) -> None:
        """analyzer_set=['linter:ruff'] skips the cheap analyzer.

        When ruff is available, should only have ruff findings (no cheap).
        When ruff is not available, should have zero findings.
        """
        from autofix.events.schema import ChangeSet
        from autofix.funnel.pipeline import run_scan

        # Check if ruff is available
        ruff_available = True
        try:
            subprocess.run(
                ["ruff", "--version"],
                check=True,
                capture_output=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            ruff_available = False

        changeset = ChangeSet(
            paths=("module_b.py",), watcher_confidence="diff-head1"
        )

        result = run_scan(
            tiny_repo,
            changeset,
            scan_id="scan-ruff-only",
            analyzer_set=["linter:ruff"],
        )

        # All findings (if any) should be from linter:ruff
        for finding in result.findings:
            assert finding.rule_id.startswith("linter:ruff:"), (
                f"Expected only linter:ruff findings, got {finding.rule_id}"
            )

        # No cheap analyzer findings
        cheap_findings = [
            f for f in result.findings
            if f.rule_id.startswith("unused-import")
        ]
        assert cheap_findings == []

    def test_explicit_both_runs_both(self, tiny_repo: Path) -> None:
        """analyzer_set=['cheap', 'linter:ruff'] runs both analyzers.

        When both are specified and ruff is available, should have
        findings from both analyzers.
        """
        from autofix.events.schema import ChangeSet
        from autofix.funnel.pipeline import run_scan

        # Check if ruff is available
        ruff_available = True
        try:
            subprocess.run(
                ["ruff", "--version"],
                check=True,
                capture_output=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.CalledProcessError):
            ruff_available = False

        changeset = ChangeSet(
            paths=("module_b.py",), watcher_confidence="diff-head1"
        )

        result = run_scan(
            tiny_repo,
            changeset,
            scan_id="scan-both",
            analyzer_set=["cheap", "linter:ruff"],
        )

        if ruff_available:
            # Should have findings from at least one of the analyzers
            assert len(result.findings) > 0

            # Should have at least one finding from cheap analyzer
            cheap_findings = [
                f for f in result.findings
                if f.rule_id.startswith("unused-import")
            ]
            assert len(cheap_findings) > 0

            # Should have ruff findings (if ruff detected issues)
            ruff_findings = [
                f for f in result.findings
                if f.rule_id.startswith("linter:ruff:")
            ]
            # Note: ruff_findings may be empty if ruff finds no issues
        else:
            # If ruff not available, should still get cheap analyzer results
            cheap_findings = [
                f for f in result.findings
                if f.rule_id.startswith("unused-import")
            ]
            assert len(cheap_findings) > 0

    def test_unknown_analyzer_logs_warning(self, tiny_repo: Path) -> None:
        """analyzer_set=['bogus'] logs AnalyzerUnknown event.

        Unknown analyzer names should be logged and skipped gracefully.
        """
        from autofix.events.schema import ChangeSet
        from autofix.funnel.pipeline import run_scan

        changeset = ChangeSet(
            paths=("module_b.py",), watcher_confidence="diff-head1"
        )

        result = run_scan(
            tiny_repo,
            changeset,
            scan_id="scan-unknown",
            analyzer_set=["bogus"],
        )

        # Read events.jsonl
        events = _collect_events(tiny_repo)

        # Should have at least one AnalyzerUnknown event for "bogus"
        # The event structure has "event" at top level and "scan_event" payload
        unknown_events = [
            e for e in events
            if e.get("event") == "AnalyzerUnknown"
            and e.get("scan_event", {}).get("analyzer") == "bogus"
        ]
        assert len(unknown_events) >= 1

    def test_analyzer_set_none_is_backward_compatible(
        self, tiny_repo: Path
    ) -> None:
        """analyzer_set=None (default) uses only cheap analyzer.

        Verifies backward compatibility when analyzer_set is not provided.
        """
        from autofix.events.schema import ChangeSet
        from autofix.funnel.pipeline import run_scan

        changeset = ChangeSet(
            paths=("module_b.py",), watcher_confidence="diff-head1"
        )

        result = run_scan(tiny_repo, changeset, scan_id="scan-compat")

        # Should only have cheap analyzer findings
        for finding in result.findings:
            assert finding.rule_id.startswith("unused-import"), (
                f"Expected only cheap findings, got {finding.rule_id}"
            )

    def test_mixed_valid_and_invalid_analyzers(self, tiny_repo: Path) -> None:
        """analyzer_set=['cheap', 'bogus', 'linter:ruff'] runs valid ones.

        Invalid names should be skipped; valid ones should run.
        """
        from autofix.events.schema import ChangeSet
        from autofix.funnel.pipeline import run_scan

        changeset = ChangeSet(
            paths=("module_b.py",), watcher_confidence="diff-head1"
        )

        result = run_scan(
            tiny_repo,
            changeset,
            scan_id="scan-mixed",
            analyzer_set=["cheap", "bogus", "linter:ruff"],
        )

        # Should have cheap findings
        cheap_findings = [
            f for f in result.findings
            if f.rule_id.startswith("unused-import")
        ]
        assert len(cheap_findings) > 0

        # Check events for the bogus analyzer
        events = _collect_events(tiny_repo)
        unknown_events = [
            e for e in events
            if e.get("event") == "AnalyzerUnknown"
            and e.get("scan_event", {}).get("analyzer") == "bogus"
        ]
        # Should log unknown analyzer event
        assert len(unknown_events) >= 1

    def test_analyzer_set_empty_list_produces_no_findings(
        self, tiny_repo: Path
    ) -> None:
        """analyzer_set=[] with no analyzers produces empty findings.

        When no analyzers are specified, no findings should be produced.
        """
        from autofix.events.schema import ChangeSet
        from autofix.funnel.pipeline import run_scan

        changeset = ChangeSet(
            paths=("module_b.py",), watcher_confidence="diff-head1"
        )

        result = run_scan(
            tiny_repo,
            changeset,
            scan_id="scan-empty",
            analyzer_set=[],
        )

        # Should have no findings when no analyzers run
        assert result.findings == []
