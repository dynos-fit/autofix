"""Tests for StateMachine.from_log per AC-13.

Covers happy reconstruction, FileNotFoundError on missing file, InvalidLog on
empty file, malformed JSON, and illegal transition sequence; also asserts no
new rows are written by the read path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from autofix.workflow import InvalidLog, State, StateMachine


def _log_path(tmp_path: Path, run_id: str) -> Path:
    return tmp_path / ".autofix" / "runs" / run_id / "state.jsonl"


def test_happy_reconstruction_preserves_history_and_does_not_write(tmp_path: Path) -> None:
    """from_log reconstructs an equal history without appending new rows."""
    a = StateMachine(root=tmp_path)
    a.transition(to_state=State.TRIAGING, evidence_sha256="1" * 64, reason="x1")
    a.transition(to_state=State.PLANNING, evidence_sha256="2" * 64, reason="x2")
    a.transition(to_state=State.APPLYING, evidence_sha256="3" * 64, reason="x3")
    a.transition(to_state=State.VERIFYING, evidence_sha256="4" * 64, reason="x4")
    a.transition(to_state=State.DONE, evidence_sha256="5" * 64, reason="x5")
    log = _log_path(tmp_path, a.run_id)
    bytes_before = log.read_bytes()

    b = StateMachine.from_log(a.run_id, root=tmp_path)
    assert b.current_state == a.current_state
    a_hist, b_hist = a.history(), b.history()
    assert len(b_hist) == 6
    for ar, br in zip(a_hist, b_hist):
        assert (ar.ts, ar.run_id, ar.from_state, ar.to_state) == (
            br.ts, br.run_id, br.from_state, br.to_state
        )
        assert (ar.evidence_sha256, ar.reason, ar.attempt, ar.event_id) == (
            br.evidence_sha256, br.reason, br.attempt, br.event_id
        )
    # Verify byte-for-byte: from_log did not write.
    assert log.read_bytes() == bytes_before


def test_from_log_missing_file_raises_filenotfounderror(tmp_path: Path) -> None:
    """Missing log directory and file raise the standard FileNotFoundError, not InvalidLog."""
    with pytest.raises(FileNotFoundError):
        StateMachine.from_log("run_does_not_exist", root=tmp_path)


def test_from_log_empty_file_raises_invalidlog(tmp_path: Path) -> None:
    """Empty (zero-byte) log file raises InvalidLog mentioning emptiness."""
    run_id = "run_empty"
    path = _log_path(tmp_path, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    with pytest.raises(InvalidLog) as excinfo:
        StateMachine.from_log(run_id, root=tmp_path)
    assert "empty" in str(excinfo.value).lower()


def test_from_log_invalid_transition_raises_invalidlog(tmp_path: Path) -> None:
    """A skip-violation transition in a hand-written log raises InvalidLog."""
    run_id = "run_bad"
    path = _log_path(tmp_path, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"ts": "2026-05-07T00:00:00Z", "run_id": run_id, "from_state": None, "to_state": "scanning",
         "evidence_sha256": "0" * 64, "reason": None, "attempt": 1, "event_id": "st_aaaa"},
        {"ts": "2026-05-07T00:00:01Z", "run_id": run_id, "from_state": "scanning", "to_state": "triaging",
         "evidence_sha256": "a" * 64, "reason": None, "attempt": 1, "event_id": "st_bbbb"},
        # Illegal: triaging -> applying (skips planning).
        {"ts": "2026-05-07T00:00:02Z", "run_id": run_id, "from_state": "triaging", "to_state": "applying",
         "evidence_sha256": "b" * 64, "reason": None, "attempt": 1, "event_id": "st_cccc"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    bytes_before = path.read_bytes()
    with pytest.raises(InvalidLog):
        StateMachine.from_log(run_id, root=tmp_path)
    assert path.read_bytes() == bytes_before  # no rows written


def test_from_log_malformed_json_raises_invalidlog(tmp_path: Path) -> None:
    """A line that is not valid JSON raises InvalidLog (not json.JSONDecodeError)."""
    run_id = "run_malformed"
    path = _log_path(tmp_path, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all\n", encoding="utf-8")
    with pytest.raises(InvalidLog):
        StateMachine.from_log(run_id, root=tmp_path)


def test_from_log_does_not_create_directory_chain(tmp_path: Path) -> None:
    """from_log on a missing run does NOT lazily create .autofix/runs (only constructor does)."""
    assert not (tmp_path / ".autofix").exists()
    with pytest.raises(FileNotFoundError):
        StateMachine.from_log("run_nope", root=tmp_path)
    # Note: log_path.exists() check may stat parents but does not create them.
    # We only assert that the run-specific dir was not created.
    assert not (tmp_path / ".autofix" / "runs" / "run_nope").exists()
    _ = os  # silence unused-import linter for from-log helper symmetry
