"""Tests for autofix.output module.

Covers acceptance criterion: 21 (human-readable vs JSON output).
"""

from __future__ import annotations

import json

import pytest

from autofix.output import (
    format_findings,
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
