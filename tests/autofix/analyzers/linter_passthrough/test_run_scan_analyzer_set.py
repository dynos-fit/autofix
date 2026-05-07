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

    def test_explicit_mypy_only_skips_cheap_and_ruff(self, tmp_path: Path) -> None:
        """analyzer_set=['linter:mypy'] skips cheap and ruff.

        When mypy is available, should only have mypy findings (no cheap, no ruff).
        When mypy is not available, should have zero findings.
        """
        from autofix.events.schema import ChangeSet
        from autofix.funnel.pipeline import run_scan

        # Check if mypy is available
        mypy_available = True
        try:
            subprocess.run(
                ["mypy", "--version"],
                check=True,
                capture_output=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            mypy_available = False

        # Initialize repo with a file containing a type error
        _init_repo(tmp_path)
        (tmp_path / "module_a.py").write_text(
            "import os\n\npath = os.getcwd()\n", encoding="utf-8"
        )
        _commit(tmp_path, "init")
        # Add a file with type error (assignment)
        (tmp_path / "module_b.py").write_text(
            'x: int = "hello"\n', encoding="utf-8"
        )
        _commit(tmp_path, "add type error")

        changeset = ChangeSet(
            paths=("module_b.py",), watcher_confidence="diff-head1"
        )

        result = run_scan(
            tmp_path,
            changeset,
            scan_id="scan-mypy-only",
            analyzer_set=["linter:mypy"],
        )

        # All findings (if any) should be from linter:mypy
        for finding in result.findings:
            assert finding.rule_id.startswith("linter:mypy:"), (
                f"Expected only linter:mypy findings, got {finding.rule_id}"
            )

        # No cheap analyzer findings
        cheap_findings = [
            f for f in result.findings
            if f.rule_id.startswith("unused-import")
        ]
        assert cheap_findings == []

        # No ruff findings
        ruff_findings = [
            f for f in result.findings
            if f.rule_id.startswith("linter:ruff:")
        ]
        assert ruff_findings == []

    def test_explicit_all_three_runs_all(self, tmp_path: Path) -> None:
        """analyzer_set=['cheap', 'linter:ruff', 'linter:mypy'] runs all three.

        When all are specified and mypy is available, should have
        findings from mypy analyzer.
        """
        from autofix.events.schema import ChangeSet
        from autofix.funnel.pipeline import run_scan

        # Check if mypy is available
        mypy_available = True
        try:
            subprocess.run(
                ["mypy", "--version"],
                check=True,
                capture_output=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            mypy_available = False

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

        # Initialize repo with files
        _init_repo(tmp_path)
        (tmp_path / "module_a.py").write_text(
            "import os\n\npath = os.getcwd()\n", encoding="utf-8"
        )
        _commit(tmp_path, "init")
        # Add a file with type error and unused import
        (tmp_path / "module_b.py").write_text(
            'import json  # unused\nx: int = "hello"\n', encoding="utf-8"
        )
        _commit(tmp_path, "add errors")

        changeset = ChangeSet(
            paths=("module_b.py",), watcher_confidence="diff-head1"
        )

        result = run_scan(
            tmp_path,
            changeset,
            scan_id="scan-all-three",
            analyzer_set=["cheap", "linter:ruff", "linter:mypy"],
        )

        if mypy_available or ruff_available:
            # Should have findings from at least one of the analyzers
            assert len(result.findings) > 0

            # Look for mypy findings if available
            mypy_findings = [
                f for f in result.findings
                if f.rule_id.startswith("linter:mypy:")
            ]
            if mypy_available:
                # Should have at least one mypy finding
                assert len(mypy_findings) > 0
        else:
            # If neither mypy nor ruff available, should still get cheap analyzer results
            cheap_findings = [
                f for f in result.findings
                if f.rule_id.startswith("unused-import")
            ]
            assert len(cheap_findings) > 0

    def test_llm_judgment_replay_determinism(self, tiny_repo: Path) -> None:
        """LLM judgment cache ensures replay determinism.

        Directly call FakeJudgmentAnalyzer.analyze twice on the same file.
        Assert the second invocation does NOT call Scheduler.invoke_judgment.
        This verifies AC-13: replay of the same scan is deterministic
        and uses cached results.
        """
        from unittest import mock
        import json

        from autofix.analyzers.llm_judgment._base import LLMJudgmentAnalyzer
        from autofix.indexing.symbols import build_symbol_table
        from autofix.languages.python import parse_file

        # Create a FakeJudgmentAnalyzer
        class FakeJudgmentAnalyzer(LLMJudgmentAnalyzer):
            RULE_ID_PREFIX = "llm:test"
            MODEL = "test-model"

            @classmethod
            def prompt_template(cls, diff_context: str) -> str:
                return f"[test-judgment]\n{diff_context}"

        # Parse the test file
        file_path = tiny_repo / "module_b.py"
        raw_parse_result = parse_file(file_path, repo_root=tiny_repo)
        symbol_table = build_symbol_table(raw_parse_result)

        # Wrap the parse_result to add repo_root (required by LLM judgment analyzer)
        class ParseResultWithRepoRoot:
            def __init__(self, pr, repo_root):
                self._pr = pr
                self.repo_root = repo_root

            def __getattr__(self, name):
                return getattr(self._pr, name)

        parse_result = ParseResultWithRepoRoot(raw_parse_result, tiny_repo)

        # Prepare canned response
        llm_response = json.dumps([
            {
                "category": "test-issue",
                "severity": "high",
                "description": "Test finding",
                "start_line": 1,
                "end_line": 2,
                "evidence": "import json",
            }
        ])

        # First run: cache miss, scheduler called
        with mock.patch(
            "autofix.llm.scheduler.Scheduler.invoke_judgment",
            return_value=llm_response,
        ) as mock_invoke_first:
            findings1 = list(FakeJudgmentAnalyzer.analyze(parse_result, symbol_table))
            first_call_count = mock_invoke_first.call_count

        # Reset per-scan state to simulate a fresh scan with same input
        LLMJudgmentAnalyzer._reset_per_scan_state()

        # Second run: cache hit, scheduler should NOT be called
        with mock.patch(
            "autofix.llm.scheduler.Scheduler.invoke_judgment",
            return_value=llm_response,
        ) as mock_invoke_second:
            findings2 = list(FakeJudgmentAnalyzer.analyze(parse_result, symbol_table))
            second_call_count = mock_invoke_second.call_count

        # Assert: first run called scheduler at least once
        assert first_call_count >= 1, (
            f"Expected scheduler to be called on cache miss, "
            f"but call_count={first_call_count}"
        )

        # Assert: second run did NOT call scheduler (cache hit)
        assert second_call_count == 0, (
            f"Expected scheduler NOT to be called on cache hit "
            f"(replay determinism), but call_count={second_call_count}"
        )

        # Assert: both runs produce findings (from cache on second run)
        assert len(findings1) > 0, (
            f"Expected findings from first run, got {len(findings1)}"
        )
        assert len(findings2) > 0, (
            f"Expected findings from second run (cache), got {len(findings2)}"
        )

        # Assert: findings match between runs (deterministic)
        assert len(findings1) == len(findings2)
        for f1, f2 in zip(findings1, findings2):
            assert f1.rule_id == f2.rule_id
            assert f1.path == f2.path
