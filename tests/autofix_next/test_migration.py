"""Unit tests for autofix_next.migration.load_legacy_findings.

Covers ACs 2, 3, 4, 5, 6, 7, 14 (status filter, field mapping + fallbacks,
missing file, corrupt JSON, skip-on-missing-finding_id, fingerprint stability,
and delegation to autofix.state.load_findings).

These tests are authored TDD-first: they FAIL at import until
autofix_next/migration.py is created by the production code segment.
"""
from __future__ import annotations

import json
import pytest

# This import MUST raise ImportError until production code lands.
from autofix_next.migration import load_legacy_findings  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_findings(path, findings: list[dict]) -> None:
    """Write a findings.json in the legacy format the loader expects."""
    path.write_text(json.dumps({"findings": findings}), encoding="utf-8")


def _make_legacy_finding(
    finding_id: str = "fp_abc123",
    status: str = "new",
    category: str = "unused-import",
    description: str = "Unused import os",
    evidence: dict | None = None,
    confidence_score: float = 1.0,
) -> dict:
    base = {
        "finding_id": finding_id,
        "status": status,
        "category": category,
        "description": description,
        "confidence_score": confidence_score,
    }
    if evidence is not None:
        base["evidence"] = evidence
    return base


# ---------------------------------------------------------------------------
# AC 5: status filter
# ---------------------------------------------------------------------------

def test_status_filter_drops_fixed_and_issue_opened(tmp_path):
    # AC 5
    """load_legacy_findings drops entries with status 'fixed' or 'issue-opened'
    and retains entries with status 'new', 'failed', or any unrecognised string.
    """
    autofix_dir = tmp_path / ".autofix" / "state" / "current"
    autofix_dir.mkdir(parents=True)
    findings_file = autofix_dir / "findings.json"

    entries = [
        _make_legacy_finding("fp_new",     status="new",          evidence={"file": "a.py", "line": 1, "symbol": "os"}),
        _make_legacy_finding("fp_fixed",   status="fixed",        evidence={"file": "b.py", "line": 2, "symbol": "re"}),
        _make_legacy_finding("fp_issue",   status="issue-opened", evidence={"file": "c.py", "line": 3, "symbol": "sys"}),
        _make_legacy_finding("fp_failed",  status="failed",       evidence={"file": "d.py", "line": 4, "symbol": "os"}),
        _make_legacy_finding("fp_unknown", status="some-unknown", evidence={"file": "e.py", "line": 5, "symbol": "io"}),
    ]
    _write_findings(findings_file, entries)

    result = load_legacy_findings(tmp_path)
    returned_ids = {f.finding_id for f in result}

    assert "fp_new" in returned_ids,     "status=new must be retained"
    assert "fp_failed" in returned_ids,  "status=failed must be retained"
    assert "fp_unknown" in returned_ids, "unrecognised status must be retained"
    assert "fp_fixed" not in returned_ids,  "status=fixed must be dropped"
    assert "fp_issue" not in returned_ids,  "status=issue-opened must be dropped"
    assert len(result) == 3


# ---------------------------------------------------------------------------
# AC 7: field mapping — happy path (evidence present)
# ---------------------------------------------------------------------------

def test_field_mapping_uses_evidence_when_present(tmp_path):
    # AC 7
    """Each projected CandidateFinding carries the correct field values when
    all evidence keys are present.
    """
    autofix_dir = tmp_path / ".autofix" / "state" / "current"
    autofix_dir.mkdir(parents=True)
    findings_file = autofix_dir / "findings.json"

    entry = _make_legacy_finding(
        finding_id="fp_full",
        status="new",
        category="unused-import",
        description="Unused import os in module",
        evidence={"file": "src/module.py", "line": 42, "symbol": "os"},
        confidence_score=0.8,
    )
    _write_findings(findings_file, [entry])

    result = load_legacy_findings(tmp_path)
    assert len(result) == 1
    cf = result[0]

    assert cf.rule_id == "unused-import"
    assert cf.path == "src/module.py"
    assert cf.symbol_name == "os"
    assert cf.normalized_import == ""
    assert cf.start_line == 42
    assert cf.end_line == 42
    # changed_slice: f"{file}:{line}@{symbol}"
    assert cf.changed_slice == "src/module.py:42@os"
    assert cf.finding_id == "fp_full"
    assert abs(cf.analyzer_confidence - 0.8) < 1e-9


# ---------------------------------------------------------------------------
# AC 7: field mapping — fallbacks
# ---------------------------------------------------------------------------

def test_field_mapping_falls_back_when_evidence_sparse(tmp_path):
    # AC 7 (fallbacks: missing evidence, missing line, non-int line, missing symbol)
    """The projection applies all documented fallbacks without raising KeyError."""
    autofix_dir = tmp_path / ".autofix" / "state" / "current"
    autofix_dir.mkdir(parents=True)
    findings_file = autofix_dir / "findings.json"

    entries = [
        # 1. Entirely missing evidence key
        {
            "finding_id": "fp_no_evidence",
            "status": "new",
            "category": "rule-a",
            "description": "A description that is longer than sixty four characters and will be truncated",
            "confidence_score": 1.0,
        },
        # 2. Evidence present but missing 'line' key
        _make_legacy_finding(
            "fp_no_line", status="new", category="rule-b",
            description="desc",
            evidence={"file": "f.py", "symbol": "sym"},
        ),
        # 3. Evidence with non-int (string) line
        _make_legacy_finding(
            "fp_str_line", status="new", category="rule-c",
            description="desc",
            evidence={"file": "g.py", "line": "not-an-int", "symbol": "sym2"},
        ),
        # 4. Evidence with null line
        _make_legacy_finding(
            "fp_null_line", status="new", category="rule-d",
            description="desc",
            evidence={"file": "h.py", "line": None, "symbol": "sym3"},
        ),
        # 5. Evidence with file present but missing symbol key
        _make_legacy_finding(
            "fp_no_symbol", status="new", category="rule-e",
            description="my description",
            evidence={"file": "i.py", "line": 7},
        ),
        # 6. Evidence with empty file key (changed_slice should be "")
        _make_legacy_finding(
            "fp_empty_file", status="new", category="rule-f",
            description="desc",
            evidence={"file": "", "line": 3, "symbol": "sym4"},
        ),
    ]
    _write_findings(findings_file, entries)

    result = load_legacy_findings(tmp_path)
    by_id = {f.finding_id: f for f in result}
    assert len(result) == 6, f"Expected 6 findings, got {len(result)}"

    # 1. Missing evidence: path="", symbol_name=description[:64], start/end=0, changed_slice=""
    no_ev = by_id["fp_no_evidence"]
    assert no_ev.path == ""
    assert no_ev.symbol_name == "A description that is longer than sixty four characters and wi"[:64]
    assert no_ev.start_line == 0
    assert no_ev.end_line == 0
    assert no_ev.changed_slice == ""

    # 2. Missing line → start_line=0
    assert by_id["fp_no_line"].start_line == 0
    assert by_id["fp_no_line"].end_line == 0

    # 3. Non-int line → start_line=0
    assert by_id["fp_str_line"].start_line == 0
    assert by_id["fp_str_line"].end_line == 0

    # 4. Null line → start_line=0
    assert by_id["fp_null_line"].start_line == 0
    assert by_id["fp_null_line"].end_line == 0

    # 5. Missing symbol → symbol_name falls back to description[:64], changed_slice uses "" for symbol
    no_sym = by_id["fp_no_symbol"]
    assert no_sym.symbol_name == "my description"[:64]
    # changed_slice: file is non-empty so f"{file}:{line}@{symbol}" where symbol=""
    assert no_sym.changed_slice == "i.py:7@"

    # 6. Empty file → changed_slice=""
    ef = by_id["fp_empty_file"]
    assert ef.changed_slice == ""


# ---------------------------------------------------------------------------
# AC 6: skip entries missing finding_id
# ---------------------------------------------------------------------------

def test_skips_entries_missing_finding_id(tmp_path):
    # AC 6
    """Entries that lack the 'finding_id' key are skipped.
    The log callable is invoked once per skipped entry with a message
    containing 'missing finding_id'.
    """
    autofix_dir = tmp_path / ".autofix" / "state" / "current"
    autofix_dir.mkdir(parents=True)
    findings_file = autofix_dir / "findings.json"

    entries = [
        # Valid entry
        _make_legacy_finding("fp_valid", status="new", evidence={"file": "a.py", "line": 1, "symbol": "os"}),
        # No finding_id key
        {"status": "new", "category": "x", "description": "no id here", "evidence": {}},
        # Also no finding_id
        {"status": "new", "category": "y", "description": "no id either", "evidence": {}},
    ]
    _write_findings(findings_file, entries)

    log_messages: list[str] = []
    result = load_legacy_findings(tmp_path, log=log_messages.append)

    # Only the valid entry must be returned
    assert len(result) == 1
    assert result[0].finding_id == "fp_valid"

    # Log was called once per skipped entry (2 entries missing finding_id)
    skip_logs = [m for m in log_messages if "missing finding_id" in m]
    assert len(skip_logs) == 2, (
        f"Expected 2 'missing finding_id' log messages, got {len(skip_logs)}: {log_messages}"
    )


# ---------------------------------------------------------------------------
# AC 3: missing findings.json returns []
# ---------------------------------------------------------------------------

def test_missing_findings_file_returns_empty(tmp_path):
    # AC 3
    """When the legacy findings.json is absent, load_legacy_findings returns []
    and does not raise.
    """
    # Provide a root directory with NO .autofix/state at all
    result = load_legacy_findings(tmp_path)
    assert result == []


# ---------------------------------------------------------------------------
# AC 4: corrupt JSON returns [] and logs
# ---------------------------------------------------------------------------

def test_corrupt_json_returns_empty_and_logs(tmp_path):
    # AC 4
    """When findings.json contains malformed JSON, load_legacy_findings returns [],
    calls log exactly once with a message containing 'could not load findings file',
    and does not raise.
    """
    autofix_dir = tmp_path / ".autofix" / "state" / "current"
    autofix_dir.mkdir(parents=True)
    findings_file = autofix_dir / "findings.json"
    findings_file.write_text("{this is not valid json!!!", encoding="utf-8")

    log_messages: list[str] = []
    result = load_legacy_findings(tmp_path, log=log_messages.append)

    assert result == []
    assert len(log_messages) == 1, (
        f"Expected exactly 1 log call, got {len(log_messages)}: {log_messages}"
    )
    assert "could not load findings file" in log_messages[0], (
        f"Log message missing expected substring: {log_messages[0]!r}"
    )


# ---------------------------------------------------------------------------
# AC 14e: fingerprint stability across calls
# ---------------------------------------------------------------------------

def test_fingerprint_stability_across_calls(tmp_path):
    # AC 14e
    """Two consecutive load_legacy_findings calls on identical input produce
    byte-identical finding_id sequences (no randomness introduced by the loader).
    """
    autofix_dir = tmp_path / ".autofix" / "state" / "current"
    autofix_dir.mkdir(parents=True)
    findings_file = autofix_dir / "findings.json"

    entries = [
        _make_legacy_finding("fp_stable_1", status="new", evidence={"file": "x.py", "line": 1, "symbol": "a"}),
        _make_legacy_finding("fp_stable_2", status="new", evidence={"file": "y.py", "line": 2, "symbol": "b"}),
    ]
    _write_findings(findings_file, entries)

    first_call  = [f.finding_id for f in load_legacy_findings(tmp_path)]
    second_call = [f.finding_id for f in load_legacy_findings(tmp_path)]

    assert first_call == second_call, (
        f"finding_id sequences differ between calls: {first_call!r} vs {second_call!r}"
    )
    assert first_call == ["fp_stable_1", "fp_stable_2"]


# ---------------------------------------------------------------------------
# AC 2: delegation to autofix.state.load_findings (CON-04)
# ---------------------------------------------------------------------------

def test_delegates_to_legacy_load_findings(tmp_path, monkeypatch):
    # AC 2 — spy on autofix.state.load_findings; addresses audit CON-04
    """load_legacy_findings must call autofix.state.load_findings(root, log=...)
    rather than re-implementing the JSON parse itself.
    """
    import autofix.state as legacy_state

    calls: list[tuple] = []
    original = legacy_state.load_findings

    def _spy(root, *, log=None):
        calls.append((root, log))
        return original(root, log=log)

    monkeypatch.setattr(legacy_state, "load_findings", _spy)

    # Empty directory — no findings.json; call should return [] but still delegate
    result = load_legacy_findings(tmp_path)

    assert len(calls) == 1, (
        f"Expected load_findings to be called exactly once, got {len(calls)}"
    )
    assert calls[0][0] == tmp_path, (
        f"Expected root={tmp_path!r}, got {calls[0][0]!r}"
    )
    assert result == []
