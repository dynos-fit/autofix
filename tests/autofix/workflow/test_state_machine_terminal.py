"""Tests for terminal-state behaviour per AC-4, AC-8 (DONE/FAILED reject all transitions).

After driving the machine into either terminal state, every attempt to move to
any other state raises InvalidTransition and does not mutate disk or memory.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from autofix.workflow import InvalidTransition, State, StateMachine


def _log_path(tmp_path: Path, sm: StateMachine) -> Path:
    return tmp_path / ".autofix" / "runs" / sm.run_id / "state.jsonl"


def _line_count(path: Path) -> int:
    return len([ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()])


def _drive_to_done(sm: StateMachine) -> None:
    for to_s in (State.TRIAGING, State.PLANNING, State.APPLYING, State.VERIFYING, State.DONE):
        sm.transition(to_state=to_s, evidence_sha256="f" * 64)


def _drive_to_failed(sm: StateMachine) -> None:
    sm.transition(to_state=State.FAILED, evidence_sha256="f" * 64)


_OTHERS_FOR_DONE = [s for s in State if s is not State.DONE]
_OTHERS_FOR_FAILED = [s for s in State if s is not State.FAILED]


@pytest.mark.parametrize("target", _OTHERS_FOR_DONE)
def test_done_rejects_every_transition(tmp_path: Path, target: State) -> None:
    """From DONE, every transition (including to DONE itself absent here) raises and does not mutate."""
    sm = StateMachine(root=tmp_path)
    _drive_to_done(sm)
    log = _log_path(tmp_path, sm)
    lines_before = _line_count(log)
    bytes_before = log.read_bytes()
    assert sm.current_state is State.DONE
    with pytest.raises(InvalidTransition):
        sm.transition(to_state=target, evidence_sha256="a" * 64)
    assert sm.current_state is State.DONE
    assert _line_count(log) == lines_before
    assert log.read_bytes() == bytes_before
    # Sanity: the persisted final row really is DONE.
    last = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert last["to_state"] == "done"


@pytest.mark.parametrize("target", _OTHERS_FOR_FAILED)
def test_failed_rejects_every_transition(tmp_path: Path, target: State) -> None:
    """From FAILED, every transition raises InvalidTransition and does not mutate."""
    sm = StateMachine(root=tmp_path)
    _drive_to_failed(sm)
    log = _log_path(tmp_path, sm)
    lines_before = _line_count(log)
    bytes_before = log.read_bytes()
    assert sm.current_state is State.FAILED
    with pytest.raises(InvalidTransition):
        sm.transition(to_state=target, evidence_sha256="a" * 64)
    assert sm.current_state is State.FAILED
    assert _line_count(log) == lines_before
    assert log.read_bytes() == bytes_before
