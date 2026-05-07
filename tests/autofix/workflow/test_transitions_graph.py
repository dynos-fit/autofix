"""Tests for the directed transition graph from AC-4.

Locks the per-state out-edge frozensets and the derived O(1) pair set.
"""
from __future__ import annotations

import pytest

from autofix.workflow import State
from autofix.workflow.state_machine import _TRANSITION_PAIRS, _TRANSITIONS


def test_transitions_keys_are_all_nine_states() -> None:
    """Every key in _TRANSITIONS is one of the 9 enum members."""
    assert set(_TRANSITIONS.keys()) == set(State)


# Per-state expected out-edge frozensets, reproduced verbatim from AC-4.
_EXPECTED_OUT_EDGES: dict[State, frozenset[State]] = {
    State.SCANNING: frozenset({State.TRIAGING, State.FAILED}),
    State.TRIAGING: frozenset({State.PLANNING, State.HUMAN_REVIEW, State.FAILED}),
    State.PLANNING: frozenset({State.APPLYING, State.HUMAN_REVIEW, State.FAILED}),
    State.APPLYING: frozenset(
        {State.VERIFYING, State.RETRY, State.HUMAN_REVIEW, State.FAILED}
    ),
    State.VERIFYING: frozenset(
        {State.DONE, State.RETRY, State.HUMAN_REVIEW, State.FAILED}
    ),
    State.RETRY: frozenset(
        {State.TRIAGING, State.PLANNING, State.APPLYING, State.FAILED}
    ),
    State.HUMAN_REVIEW: frozenset(
        {State.PLANNING, State.APPLYING, State.DONE, State.FAILED}
    ),
    State.DONE: frozenset(),
    State.FAILED: frozenset(),
}


@pytest.mark.parametrize("state", list(State))
def test_each_state_out_edges_match_spec(state: State) -> None:
    """Per-state frozenset matches AC-4 verbatim."""
    assert _TRANSITIONS[state] == _EXPECTED_OUT_EDGES[state]


def test_transition_pairs_cardinality_equals_sum_of_out_degrees() -> None:
    """The flat pair set has cardinality equal to the sum of per-state out-degrees."""
    expected = sum(len(s) for s in _TRANSITIONS.values())
    assert len(_TRANSITION_PAIRS) == expected


def test_every_legal_pair_present() -> None:
    """Every (from, to) pair from _TRANSITIONS is in _TRANSITION_PAIRS."""
    for from_s, targets in _TRANSITIONS.items():
        for to_s in targets:
            assert (from_s, to_s) in _TRANSITION_PAIRS


# Illegal pairs: terminal-out, skip violations, backwards.
_ILLEGAL_PAIRS: list[tuple[State, State]] = [
    # Terminal-out (DONE has no out-edges).
    (State.DONE, State.SCANNING),
    (State.DONE, State.PLANNING),
    (State.DONE, State.FAILED),
    # Terminal-out (FAILED has no out-edges).
    (State.FAILED, State.SCANNING),
    (State.FAILED, State.DONE),
    # Skip violations (must traverse intermediate states).
    (State.SCANNING, State.PLANNING),
    (State.SCANNING, State.APPLYING),
    (State.SCANNING, State.DONE),
    # Backward violations (graph is forward-only outside RETRY).
    (State.PLANNING, State.SCANNING),
    (State.APPLYING, State.TRIAGING),
]


@pytest.mark.parametrize(("from_s", "to_s"), _ILLEGAL_PAIRS)
def test_illegal_pair_absent(from_s: State, to_s: State) -> None:
    """Sample illegal pairs are NOT in _TRANSITION_PAIRS."""
    assert (from_s, to_s) not in _TRANSITION_PAIRS
