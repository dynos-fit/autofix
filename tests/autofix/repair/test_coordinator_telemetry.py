"""Telemetry envelope tests for ``coordinate_repairs``.

Covers AC-10 (envelope shape, dedup per call, only no_mapping emits) and
AC-11 (purity when ``root=None`` — zero filesystem IO).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from autofix.evidence.schema import CandidateFinding
from autofix.repair import coordinate_repairs


def _finding(rule_id: str, *, analyzer_confidence: float = 1.0, fid: str | None = None) -> CandidateFinding:
    return CandidateFinding(
        rule_id,
        "pkg/mod.py",
        "my_func",
        "os",
        1,
        1,
        "",
        fid or f"fp_{rule_id}",
        analyzer_confidence=analyzer_confidence,
    )


def test_root_none_performs_no_filesystem_io(tmp_path: Path, monkeypatch) -> None:
    """AC-11: with ``root=None`` the function does not create ``.autofix/``.

    We chdir to a clean tmp_path so any accidental ``./.autofix`` write would
    be visible after the call.
    """
    monkeypatch.chdir(tmp_path)
    finding = _finding("custom:unmapped:thing")
    tasks = coordinate_repairs([finding], threshold=0.5, root=None)

    assert len(tasks) == 1
    assert not (tmp_path / ".autofix").exists(), (
        "root=None must be pure: no .autofix directory may be created"
    )


def test_unique_unmapped_rule_ids_emit_one_envelope_each(tmp_path: Path) -> None:
    """AC-10: dedup per call. Two findings sharing one unmapped rule_id +
    one finding with a different unmapped rule_id => exactly 2 envelopes.

    A mapped finding must NOT emit anything.
    """
    findings = [
        _finding("custom:weird:rule", fid="a"),
        _finding("custom:weird:rule", fid="b"),  # duplicate unmapped rule_id
        _finding("custom:other:rule", fid="c"),
        _finding("linter:ruff:F401", fid="d"),  # mapped -> no envelope
    ]

    coordinate_repairs(findings, threshold=0.5, root=tmp_path)

    events_file = tmp_path / ".autofix" / "events.jsonl"
    assert events_file.exists(), "events.jsonl must be created under <root>/.autofix/"

    lines = events_file.read_text().splitlines()
    assert len(lines) == 2, (
        f"expected exactly 2 envelopes (one per unique unmapped rule_id), got {len(lines)}: {lines!r}"
    )

    parsed = [json.loads(line) for line in lines]
    rule_ids_emitted = {p["rule_id"] for p in parsed}
    assert rule_ids_emitted == {"custom:weird:rule", "custom:other:rule"}


def test_envelope_shape_exactly_three_keys(tmp_path: Path) -> None:
    """Envelope keys are exactly ``{event, rule_id, ts}`` — no extras, no missing.
    ``event`` is the literal ``"UnmappedRuleTier"``; ``ts`` parses as ISO 8601.
    """
    finding = _finding("custom:shape:check")
    coordinate_repairs([finding], threshold=0.5, root=tmp_path)

    events_file = tmp_path / ".autofix" / "events.jsonl"
    [line] = events_file.read_text().splitlines()
    envelope = json.loads(line)

    assert set(envelope.keys()) == {"event", "rule_id", "ts"}, (
        f"envelope must have exactly 3 keys, got {sorted(envelope.keys())!r}"
    )
    assert envelope["event"] == "UnmappedRuleTier"
    assert envelope["rule_id"] == "custom:shape:check"
    # ts must round-trip through datetime.fromisoformat.
    parsed_ts = datetime.fromisoformat(envelope["ts"])
    assert parsed_ts.tzinfo is not None, "ts must include timezone info"


def test_below_threshold_findings_do_not_emit_envelopes(tmp_path: Path) -> None:
    """AC-10: only ``no_mapping`` findings emit envelopes; below_threshold does not.

    A below-threshold finding with a deterministic rule_id is NOT unmapped.
    """
    findings = [
        # Below-threshold deterministic rule -> demoted to HUMAN_ONLY/below_threshold.
        _finding("linter:ruff:F401", analyzer_confidence=0.1, fid="bt"),
        # Below-threshold UNMAPPED rule -> still demoted to below_threshold.
        # (Per AC-7 the confidence gate runs FIRST, so reason is below_threshold,
        # not no_mapping; it must therefore NOT emit an envelope.)
        _finding("custom:never:see", analyzer_confidence=0.1, fid="bt2"),
    ]

    coordinate_repairs(findings, threshold=0.5, root=tmp_path)

    events_file = tmp_path / ".autofix" / "events.jsonl"
    # Either the file does not exist, or it exists but is empty. Both satisfy
    # "no envelopes for below_threshold findings". The implementation only
    # creates the directory when there ARE unmapped envelopes to write.
    if events_file.exists():
        assert events_file.read_text() == "", (
            f"no envelopes should be written for below_threshold findings; got {events_file.read_text()!r}"
        )
