"""Workflow subsystem package for autofix.

This package exposes the producer-only state machine that records every
transition of a single autofix run as an append-only JSONL log at::

    <root>/.autofix/runs/<run_id>/state.jsonl

Public surface
--------------
- ``State``            — 9-member str-mixin enum of workflow states.
- ``StateMachine``     — validates transitions and persists rows.
- ``StateRow``         — frozen dataclass (slots=True) representing one row.
- ``InvalidTransition`` — raised on illegal (from, to) pairs.
- ``InvalidLog``        — raised when a log file is empty, malformed, or
                          contains an illegal transition sequence.

Import from this package, not from ``autofix.workflow.state_machine``
directly (the module internals like ``_TRANSITIONS`` are testable but not
part of the public surface).
"""

from autofix.workflow.state_machine import (
    InvalidLog,
    InvalidTransition,
    State,
    StateMachine,
    StateRow,
)

__all__ = ["State", "StateMachine", "StateRow", "InvalidTransition", "InvalidLog"]
