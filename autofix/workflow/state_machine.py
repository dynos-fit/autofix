"""Producer-only state machine for autofix workflow runs.

This module records every transition of a single autofix run as an
append-only sequence of JSONL rows at::

    <root>/.autofix/runs/<run_id>/state.jsonl

## Directed transition graph

The legal (from, to) state pairs are encoded in ``_TRANSITIONS`` and
derived into ``_TRANSITION_PAIRS`` for O(1) membership tests:

    SCANNING     → TRIAGING | FAILED
    TRIAGING     → PLANNING | HUMAN_REVIEW | FAILED
    PLANNING     → APPLYING | HUMAN_REVIEW | FAILED
    APPLYING     → VERIFYING | RETRY | HUMAN_REVIEW | FAILED
    VERIFYING    → DONE | RETRY | HUMAN_REVIEW | FAILED
    RETRY        → TRIAGING | PLANNING | APPLYING | FAILED
    HUMAN_REVIEW → PLANNING | APPLYING | DONE | FAILED
    DONE         → (terminal — no outgoing edges)
    FAILED       → (terminal — no outgoing edges)

## JSONL row schema (v1, no version field)

Each row is a compact JSON object with exactly 8 keys in declaration order:
``ts``, ``run_id``, ``from_state``, ``to_state``, ``evidence_sha256``,
``reason``, ``attempt``, ``event_id``.

## Append atomicity contract

Writes use ``os.open(O_WRONLY | O_CREAT | O_APPEND, 0o644)`` + ``os.write``
+ ``os.close``.  POSIX guarantees that writes ≤ PIPE_BUF (≥ 4096 bytes on
macOS/Linux) are atomic with respect to other O_APPEND writers into the same
file, so concurrent writers each using a distinct ``run_id`` directory cannot
interleave bytes within a single row.  If two threads on the SAME instance
call ``transition`` simultaneously, byte-level atomicity is preserved but the
in-memory attempt counter (AC-9) is NOT thread-safe: both rows land intact
on disk, but the ``attempt`` values may not be strictly monotonic across
threads.  This is documented, not fixed; callers that need strict ordering
should serialise their ``transition`` calls externally.

## Exception vocabulary (closed)

- ``InvalidTransition`` — (from, to) pair absent from ``_TRANSITION_PAIRS``.
- ``InvalidLog``        — log file is empty, malformed, or contains an
                         illegal transition.
- ``ValueError``        — ``evidence_sha256`` format violation.
- ``FileNotFoundError`` — workflow log file missing on ``from_log``.
- ``OSError``           — propagated unchanged from ``os.write`` on IO failure.

## run_id collision policy

If the caller supplies a ``run_id`` that matches an existing run directory,
the constructor appends a new initial row to the existing workflow log
(because ``O_APPEND``).  Callers are responsible for run_id uniqueness;
the state machine does not defend against deliberate reuse.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

# ---------------------------------------------------------------------------
# Base58 helpers (Bitcoin/IPFS alphabet — no 0, O, I, l)
# ---------------------------------------------------------------------------

_BASE58_ALPHABET: str = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_encode(data: bytes) -> str:
    """Encode *data* as a base58 string using the Bitcoin/IPFS alphabet."""
    if not data:
        return ""
    n_zero = 0
    for b in data:
        if b == 0:
            n_zero += 1
        else:
            break
    num = int.from_bytes(data, "big")
    chars: list[str] = []
    while num > 0:
        num, rem = divmod(num, 58)
        chars.append(_BASE58_ALPHABET[rem])
    chars.reverse()
    return ("1" * n_zero) + "".join(chars)


def _make_run_id() -> str:
    """Return a fresh ``run_``-prefixed identifier from UUID4 entropy."""
    return "run_" + _base58_encode(uuid.uuid4().bytes)[:10]


def _make_event_id() -> str:
    """Return a fresh ``st_``-prefixed identifier from UUID4 entropy."""
    return "st_" + _base58_encode(uuid.uuid4().bytes)[:10]


def _now_iso_z() -> str:
    """Return current UTC time as ISO-8601 with trailing ``Z``, second resolution."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class InvalidTransition(ValueError):
    """Raised when a (from_state, to_state) pair is not in _TRANSITION_PAIRS."""


class InvalidLog(ValueError):
    """Raised when a workflow log file is empty, malformed, or contains an illegal transition."""


# ---------------------------------------------------------------------------
# State enum
# ---------------------------------------------------------------------------


class State(str, Enum):
    SCANNING = "scanning"
    TRIAGING = "triaging"
    PLANNING = "planning"
    APPLYING = "applying"
    VERIFYING = "verifying"
    DONE = "done"
    RETRY = "retry"
    HUMAN_REVIEW = "human-review"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Transition graph — module-level constants (load-bearing contracts)
# ---------------------------------------------------------------------------

_TRANSITIONS: dict[State, frozenset[State]] = {
    State.SCANNING:     frozenset({State.TRIAGING, State.FAILED}),
    State.TRIAGING:     frozenset({State.PLANNING, State.HUMAN_REVIEW, State.FAILED}),
    State.PLANNING:     frozenset({State.APPLYING, State.HUMAN_REVIEW, State.FAILED}),
    State.APPLYING:     frozenset({State.VERIFYING, State.RETRY, State.HUMAN_REVIEW, State.FAILED}),
    State.VERIFYING:    frozenset({State.DONE, State.RETRY, State.HUMAN_REVIEW, State.FAILED}),
    State.RETRY:        frozenset({State.TRIAGING, State.PLANNING, State.APPLYING, State.FAILED}),
    State.HUMAN_REVIEW: frozenset({State.PLANNING, State.APPLYING, State.DONE, State.FAILED}),
    State.DONE:         frozenset(),
    State.FAILED:       frozenset(),
}

_TRANSITION_PAIRS: frozenset[tuple[State, State]] = frozenset(
    (from_state, to_state)
    for from_state, targets in _TRANSITIONS.items()
    for to_state in targets
)

# ---------------------------------------------------------------------------
# StateRow dataclass
# ---------------------------------------------------------------------------

_EVIDENCE_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{64}$")


@dataclass(slots=True, frozen=True)
class StateRow:
    ts: str
    run_id: str
    from_state: State | None
    to_state: State
    evidence_sha256: str
    reason: str | None
    attempt: int
    event_id: str


# ---------------------------------------------------------------------------
# StateMachine
# ---------------------------------------------------------------------------


class StateMachine:
    """Producer-only state machine for a single autofix run.

    Each instance owns exactly one ``run_id`` and writes its transitions to::

        <root>/.autofix/runs/<run_id>/state.jsonl

    Instances are NOT thread-safe at the in-memory counter level (see module
    docstring).  File writes are byte-level atomic per the O_APPEND contract.
    """

    def __init__(self, run_id: str | None = None, *, root: Path) -> None:
        self._run_id: str = run_id if run_id is not None else _make_run_id()
        self._root: Path = root
        self._current_state: State = State.SCANNING
        self._history: list[StateRow] = []
        # Per-state attempt counters: {state -> highest attempt written}
        self._attempts: dict[State, int] = {}

        # Create parent directory chain before the first write.
        log_path = self._log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)

        # Write the initial SCANNING row immediately.
        initial_row = StateRow(
            ts=_now_iso_z(),
            run_id=self._run_id,
            from_state=None,
            to_state=State.SCANNING,
            evidence_sha256="0" * 64,
            reason=None,
            attempt=1,
            event_id=_make_event_id(),
        )
        self._attempts[State.SCANNING] = 1
        self._append_row(initial_row)
        self._history.append(initial_row)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log_path(self) -> Path:
        return self._root / ".autofix" / "runs" / self._run_id / "state.jsonl"

    def _append_row(self, row: StateRow) -> None:
        """Write *row* as a compact JSONL line using O_APPEND semantics."""
        row_dict = {
            "ts": row.ts,
            "run_id": row.run_id,
            "from_state": row.from_state.value if row.from_state is not None else None,
            "to_state": row.to_state.value,
            "evidence_sha256": row.evidence_sha256,
            "reason": row.reason,
            "attempt": row.attempt,
            "event_id": row.event_id,
        }
        line_bytes = json.dumps(row_dict, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        path_str = str(self._log_path())
        fd = os.open(path_str, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line_bytes)
        finally:
            os.close(fd)

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def current_state(self) -> State:
        return self._current_state

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def transition(
        self,
        *,
        to_state: State,
        evidence_sha256: str,
        reason: str | None = None,
    ) -> None:
        """Validate and record a state transition.

        Raises:
            InvalidTransition: if the (current, to_state) pair is not in
                ``_TRANSITION_PAIRS``.
            ValueError: if ``evidence_sha256`` does not match ``^[0-9a-f]{64}$``.
            OSError: propagated unchanged if the JSONL write fails; in that
                case ``current_state`` is NOT updated.
        """
        # a. Validate transition legality.
        if (self._current_state, to_state) not in _TRANSITION_PAIRS:
            raise InvalidTransition(
                f"illegal transition: {self._current_state!r} -> {to_state!r}"
            )

        # b. Validate evidence_sha256 format.
        if not _EVIDENCE_RE.fullmatch(evidence_sha256):
            raise ValueError(
                f"evidence_sha256 must be a 64-char lowercase hex string; "
                f"got {evidence_sha256!r}"
            )

        # Increment attempt counter BEFORE writing (see Risk Notes in module docstring).
        attempt = self._attempts.get(to_state, 0) + 1
        self._attempts[to_state] = attempt

        row = StateRow(
            ts=_now_iso_z(),
            run_id=self._run_id,
            from_state=self._current_state,
            to_state=to_state,
            evidence_sha256=evidence_sha256,
            reason=reason,
            attempt=attempt,
            event_id=_make_event_id(),
        )

        # Write FIRST; update in-memory state only on success.
        self._append_row(row)
        self._history.append(row)
        self._current_state = to_state

    def history(self) -> list[StateRow]:
        """Return a shallow copy of the in-memory row history in insertion order."""
        return list(self._history)

    # ------------------------------------------------------------------
    # Class method: reconstruct from persisted log
    # ------------------------------------------------------------------

    @classmethod
    def from_log(cls, run_id: str, *, root: Path) -> StateMachine:
        """Reconstruct a StateMachine from a persisted workflow log file.

        Args:
            run_id: the run identifier whose log to read.
            root: repository root (same argument as the constructor).

        Returns:
            A StateMachine instance whose ``current_state`` and ``history()``
            reflect the persisted rows.  No new rows are written to disk.

        Raises:
            FileNotFoundError: if the log file does not exist.
            InvalidLog: if the file is empty, any line is malformed, or the
                transition sequence is invalid.
        """
        log_path = root / ".autofix" / "runs" / run_id / "state.jsonl"

        if not log_path.exists():
            raise FileNotFoundError(
                f"workflow log not found: {log_path}"
            )

        raw_lines = log_path.read_text(encoding="utf-8").splitlines()
        non_empty = [ln for ln in raw_lines if ln.strip()]

        if not non_empty:
            raise InvalidLog(
                f"workflow log is empty: {log_path}"
            )

        # Parse and validate each row.
        rows: list[StateRow] = []
        _REQUIRED_KEYS = {"ts", "run_id", "from_state", "to_state", "evidence_sha256", "reason", "attempt", "event_id"}

        for line_no, line in enumerate(non_empty, start=1):
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise InvalidLog(
                    f"line {line_no}: JSON parse error: {exc}"
                ) from exc

            if not isinstance(data, dict):
                raise InvalidLog(f"line {line_no}: expected JSON object, got {type(data).__name__}")

            missing = _REQUIRED_KEYS - data.keys()
            if missing:
                raise InvalidLog(f"line {line_no}: missing keys {missing!r}")

            # Validate from_state
            raw_from = data["from_state"]
            if raw_from is None:
                parsed_from: State | None = None
            else:
                try:
                    parsed_from = State(raw_from)
                except ValueError:
                    raise InvalidLog(
                        f"line {line_no}: unrecognised from_state value {raw_from!r}"
                    )

            # Validate to_state
            raw_to = data["to_state"]
            try:
                parsed_to = State(raw_to)
            except ValueError:
                raise InvalidLog(
                    f"line {line_no}: unrecognised to_state value {raw_to!r}"
                )

            # Validate attempt type
            if not isinstance(data["attempt"], int):
                raise InvalidLog(f"line {line_no}: attempt must be int, got {type(data['attempt']).__name__}")

            # Validate ts and event_id are strings
            if not isinstance(data["ts"], str):
                raise InvalidLog(f"line {line_no}: ts must be str")
            if not isinstance(data["event_id"], str):
                raise InvalidLog(f"line {line_no}: event_id must be str")
            if not isinstance(data["run_id"], str):
                raise InvalidLog(f"line {line_no}: run_id must be str")
            if data["reason"] is not None and not isinstance(data["reason"], str):
                raise InvalidLog(f"line {line_no}: reason must be str or null")

            row = StateRow(
                ts=data["ts"],
                run_id=data["run_id"],
                from_state=parsed_from,
                to_state=parsed_to,
                evidence_sha256=data["evidence_sha256"],
                reason=data["reason"],
                attempt=data["attempt"],
                event_id=data["event_id"],
            )
            rows.append(row)

        # Validate first row shape.
        first = rows[0]
        if first.from_state is not None or first.to_state != State.SCANNING:
            raise InvalidLog(
                f"first row must have from_state=null and to_state='scanning'; "
                f"got from_state={first.from_state!r}, to_state={first.to_state!r}"
            )

        # Validate subsequent rows: continuity and legal transitions.
        for i in range(1, len(rows)):
            prior = rows[i - 1]
            curr = rows[i]
            if curr.from_state != prior.to_state:
                raise InvalidLog(
                    f"row {i + 1}: from_state {curr.from_state!r} does not match "
                    f"prior to_state {prior.to_state!r}"
                )
            if (curr.from_state, curr.to_state) not in _TRANSITION_PAIRS:
                raise InvalidLog(
                    f"row {i + 1}: illegal transition "
                    f"{curr.from_state!r} -> {curr.to_state!r}"
                )

        # Reconstruct the instance WITHOUT calling __init__ (to avoid writing rows).
        instance = object.__new__(cls)
        instance._run_id = run_id
        instance._root = root
        instance._current_state = rows[-1].to_state
        instance._history = list(rows)

        # Reconstruct attempt counters from persisted rows.
        attempts: dict[State, int] = {}
        for row in rows:
            attempts[row.to_state] = max(attempts.get(row.to_state, 0), row.attempt)
        instance._attempts = attempts

        return instance


# ---------------------------------------------------------------------------
# Module-level __all__
# ---------------------------------------------------------------------------

__all__ = ["State", "StateMachine", "StateRow", "InvalidTransition", "InvalidLog"]
