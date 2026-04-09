"""Tests for autofix.output module.

Covers acceptance criterion: 21 (human-readable vs JSON output).
"""

from __future__ import annotations

import json

import pytest

from autofix.output import (
    format_config,
    format_findings,
    format_repos,
    format_scan_all_summary,
)


# ---------------------------------------------------------------------------
# Criterion 21: human-readable vs JSON for findings
# ---------------------------------------------------------------------------

class TestFormatFindings:
    """Criterion 21: --json produces JSON, default produces human-readable text."""

    SAMPLE_FINDINGS = [
        {
            "finding_id": "f1",
            "category": "llm-review",
            "description": "Possible SQL injection",
            "evidence": {"file": "app.py", "line": 42},
            "severity": "high",
            "confidence_score": 0.9,
        },
        {
            "finding_id": "f2",
            "category": "llm-review",
            "description": "Unused import",
            "evidence": {"file": "utils.py", "line": 1},
            "severity": "low",
            "confidence_score": 0.6,
        },
    ]

    def test_json_output_is_valid_json(self) -> None:
        output = format_findings(self.SAMPLE_FINDINGS, as_json=True)
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        assert len(parsed) == 2
        assert parsed[0]["finding_id"] == "f1"

    def test_human_readable_is_not_json(self) -> None:
        output = format_findings(self.SAMPLE_FINDINGS, as_json=False)
        with pytest.raises(json.JSONDecodeError):
            json.loads(output)

    def test_human_readable_contains_key_info(self) -> None:
        output = format_findings(self.SAMPLE_FINDINGS, as_json=False)
        assert "app.py" in output
        assert "SQL injection" in output

    def test_empty_findings_json(self) -> None:
        output = format_findings([], as_json=True)
        assert json.loads(output) == []

    def test_empty_findings_human_readable(self) -> None:
        output = format_findings([], as_json=False)
        assert isinstance(output, str)


# ---------------------------------------------------------------------------
# Criterion 21: human-readable vs JSON for repos
# ---------------------------------------------------------------------------

class TestFormatRepos:
    """Criterion 21: repo list supports --json."""

    SAMPLE_REPOS = [
        {"path": "/home/user/project-a"},
        {"path": "/home/user/project-b"},
    ]

    def test_json_output_is_valid_json(self) -> None:
        output = format_repos(self.SAMPLE_REPOS, as_json=True)
        parsed = json.loads(output)
        assert isinstance(parsed, list)
        assert len(parsed) == 2

    def test_human_readable_one_per_line(self) -> None:
        output = format_repos(self.SAMPLE_REPOS, as_json=False)
        lines = [line for line in output.strip().splitlines() if line.strip()]
        assert len(lines) >= 2
        assert "/home/user/project-a" in output
        assert "/home/user/project-b" in output


# ---------------------------------------------------------------------------
# Criterion 21: human-readable vs JSON for config
# ---------------------------------------------------------------------------

class TestFormatConfig:
    """Criterion 21: config show supports --json."""

    SAMPLE_CONFIG = {
        "max_files": 8,
        "interval": "30m",
        "scan_timeout": 900,
        "dry_run": False,
    }

    def test_json_output_is_valid_json(self) -> None:
        output = format_config(self.SAMPLE_CONFIG, as_json=True)
        parsed = json.loads(output)
        assert parsed["max_files"] == 8

    def test_human_readable_contains_keys_and_values(self) -> None:
        output = format_config(self.SAMPLE_CONFIG, as_json=False)
        assert "max_files" in output
        assert "8" in output
        assert "interval" in output
        assert "30m" in output


# ---------------------------------------------------------------------------
# Criterion 21: scan-all summary formatting
# ---------------------------------------------------------------------------

class TestFormatScanAllSummary:
    """Criterion 21: scan-all summary supports --json."""

    SAMPLE_SUMMARY = {
        "total": 3,
        "succeeded": 2,
        "failed": 0,
        "skipped": 1,
        "repos": [
            {"path": "/home/user/a", "status": "success"},
            {"path": "/home/user/b", "status": "success"},
            {"path": "/home/user/c", "status": "skipped", "reason": "path not found"},
        ],
    }

    def test_json_output(self) -> None:
        output = format_scan_all_summary(self.SAMPLE_SUMMARY, as_json=True)
        parsed = json.loads(output)
        assert parsed["total"] == 3

    def test_human_readable_output(self) -> None:
        output = format_scan_all_summary(self.SAMPLE_SUMMARY, as_json=False)
        assert "3" in output  # total
        assert "2" in output  # succeeded
        assert "skipped" in output.lower() or "1" in output
