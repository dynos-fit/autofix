"""Tests for StateMachine.__init__ per AC-6, AC-10, AC-15.

Covers auto-generated run_id format, custom run_id acceptance, initial row write,
and the initial in-memory state.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from autofix.workflow import State, StateMachine


_RUN_ID_RE = re.compile(r"^run_[1-9A-HJ-NP-Za-km-z]{1,10}$")
_EVENT_ID_RE = re.compile(r"^st_[1-9A-HJ-NP-Za-km-z]{1,10}$")


def _read_lines(path: Path) -> list[str]:
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_auto_generated_run_id_matches_base58_format(tmp_path: Path) -> None:
    """Auto-generated run_id matches `run_` + base58 of UUID4 (1..10 chars)."""
    sm = StateMachine(root=tmp_path)
    assert _RUN_ID_RE.fullmatch(sm.run_id) is not None


def test_initial_row_written_with_expected_shape(tmp_path: Path) -> None:
    """Constructor writes exactly one initial row with the AC-6/AC-11 shape."""
    sm = StateMachine(root=tmp_path)
    log_path = tmp_path / ".autofix" / "runs" / sm.run_id / "state.jsonl"
    assert log_path.exists()
    lines = _read_lines(log_path)
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert set(row.keys()) == {
        "ts",
        "run_id",
        "from_state",
        "to_state",
        "evidence_sha256",
        "reason",
        "attempt",
        "event_id",
    }
    assert row["from_state"] is None
    assert row["to_state"] == "scanning"
    assert row["evidence_sha256"] == "0" * 64
    assert row["reason"] is None
    assert row["attempt"] == 1
    assert _EVENT_ID_RE.fullmatch(row["event_id"]) is not None


def test_current_state_is_scanning_after_init(tmp_path: Path) -> None:
    """current_state property returns SCANNING immediately after construction."""
    sm = StateMachine(root=tmp_path)
    assert sm.current_state is State.SCANNING


def test_explicit_run_id_used_byte_for_byte(tmp_path: Path) -> None:
    """Explicit run_id passed to constructor lands at the expected path and in the row."""
    sm = StateMachine("run_test123abc", root=tmp_path)
    assert sm.run_id == "run_test123abc"
    log_path = tmp_path / ".autofix" / "runs" / "run_test123abc" / "state.jsonl"
    assert log_path.exists()
    [line] = _read_lines(log_path)
    row = json.loads(line)
    assert row["run_id"] == "run_test123abc"
