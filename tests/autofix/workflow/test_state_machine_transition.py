"""Tests for StateMachine.transition per AC-8, AC-9, AC-16.

Covers happy path, InvalidTransition, evidence_sha256 format validation,
and reason persistence + attempt counter monotonicity.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autofix.workflow import InvalidTransition, State, StateMachine


def _read_rows(path: Path) -> list[dict]:
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def _log_path(tmp_path: Path, sm: StateMachine) -> Path:
    return tmp_path / ".autofix" / "runs" / sm.run_id / "state.jsonl"


def test_happy_path_transition_persists_and_updates_state(tmp_path: Path) -> None:
    """Legal SCANNING -> TRIAGING transition writes a row and advances current_state."""
    sm = StateMachine(root=tmp_path)
    sm.transition(
        to_state=State.TRIAGING,
        evidence_sha256="a" * 64,
        reason="findings ranked",
    )
    rows = _read_rows(_log_path(tmp_path, sm))
    assert len(rows) == 2
    second = rows[1]
    assert second["from_state"] == "scanning"
    assert second["to_state"] == "triaging"
    assert second["evidence_sha256"] == "a" * 64
    assert second["reason"] == "findings ranked"
    assert second["attempt"] == 1
    assert sm.current_state is State.TRIAGING


def test_invalid_transition_raises_and_does_not_persist(tmp_path: Path) -> None:
    """Skip violation SCANNING -> PLANNING raises InvalidTransition; row not written."""
    sm = StateMachine(root=tmp_path)
    with pytest.raises(InvalidTransition) as excinfo:
        sm.transition(to_state=State.PLANNING, evidence_sha256="a" * 64)
    msg = str(excinfo.value)
    assert "SCANNING" in msg or "scanning" in msg
    assert "PLANNING" in msg or "planning" in msg
    rows = _read_rows(_log_path(tmp_path, sm))
    assert len(rows) == 1
    assert sm.current_state is State.SCANNING


@pytest.mark.parametrize(
    "bad_value",
    ["", "deadbeef", "A" * 64, "g" * 64],
    ids=["empty", "too-short", "uppercase-hex", "non-hex-char"],
)
def test_evidence_sha256_format_validation(tmp_path: Path, bad_value: str) -> None:
    """Invalid evidence_sha256 raises ValueError mentioning the field name."""
    sm = StateMachine(root=tmp_path)
    with pytest.raises(ValueError) as excinfo:
        sm.transition(to_state=State.TRIAGING, evidence_sha256=bad_value)
    assert "evidence_sha256" in str(excinfo.value)
    rows = _read_rows(_log_path(tmp_path, sm))
    assert len(rows) == 1


def test_reason_persistence_and_attempt_monotonicity(tmp_path: Path) -> None:
    """Re-entering APPLYING and RETRY produces monotonic attempt counters; reason round-trips."""
    sm = StateMachine(root=tmp_path)
    plan = [
        (State.TRIAGING, "r-tri"),
        (State.PLANNING, "r-plan"),
        (State.APPLYING, "r-app-1"),
        (State.RETRY, "r-ret-1"),
        (State.APPLYING, "r-app-2"),
        (State.RETRY, "r-ret-2"),
        (State.APPLYING, "r-app-3"),
    ]
    for to_state, reason in plan:
        sm.transition(to_state=to_state, evidence_sha256="b" * 64, reason=reason)
    rows = _read_rows(_log_path(tmp_path, sm))
    applying_rows = [r for r in rows if r["to_state"] == "applying"]
    retry_rows = [r for r in rows if r["to_state"] == "retry"]
    assert [r["attempt"] for r in applying_rows] == [1, 2, 3]
    assert [r["reason"] for r in applying_rows] == ["r-app-1", "r-app-2", "r-app-3"]
    assert [r["attempt"] for r in retry_rows] == [1, 2]
    assert [r["reason"] for r in retry_rows] == ["r-ret-1", "r-ret-2"]
