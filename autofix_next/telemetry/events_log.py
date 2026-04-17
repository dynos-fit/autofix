"""Envelope-compatible events.jsonl writer for autofix_next.

This module appends rows to ``<root>/.autofix/events.jsonl`` that coexist
byte-for-byte with legacy rows produced by
``autofix/runtime/dynos.py:log_event``. Both writers target the same file;
the file is strictly append-only and pre-existing bytes are never rewritten
(AC #11). Each new row is a single-line UTF-8 JSON object terminated by
``\\n`` with top-level keys ``event`` (string), ``at`` (ISO-8601 UTC string),
``event_id`` (base58), and ``scan_event`` (nested payload object) — see
AC #12.

Event names are constrained to the camelCase set pinned in
``autofix_next.events.schema.NEW_EVENT_NAMES``; unknown names raise
``ValueError`` at the writer boundary (AC #13) so legacy snake_case names
can never leak into the new-event namespace by accident.

Atomicity
---------
Writes use ``open(path, "a", encoding="utf-8")`` which on POSIX opens the
underlying file descriptor with ``O_APPEND``. Writes smaller than
``PIPE_BUF`` (at least 512 bytes on every POSIX platform, typically 4096 on
Linux/macOS) are guaranteed atomic with respect to other ``O_APPEND``
writers into the same file, which is the exact coexistence contract with
the legacy ``log_event`` writer. A single envelope row is well under this
limit in normal operation.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autofix_next.events.schema import NEW_EVENT_NAMES

# Base58 alphabet (Bitcoin/IPFS style: no 0, O, I, l to avoid visual ambiguity).
_BASE58_ALPHABET: str = (
    "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
)


def _now_iso_z() -> str:
    """Return the current UTC time as ISO-8601 with a trailing ``Z``.

    No fractional seconds; stable second resolution suitable for ordering
    events.jsonl rows and aligning with the legacy writer's ``now_iso``.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _base58_encode(data: bytes) -> str:
    """Minimal stdlib base58 encoder.

    Preserves leading zero bytes as leading '1' characters, per the
    canonical Bitcoin base58 spec. Returns empty string for empty input.
    """
    if not data:
        return ""
    # Count leading zero bytes; each becomes a leading '1'.
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


def _base58_event_id(nbytes: int = 8) -> str:
    """Generate an ``evt_``-prefixed base58 identifier from OS randomness.

    ``os.urandom`` is used (cryptographically-strong) to avoid collisions
    in high-throughput scan pipelines.
    """
    return "evt_" + _base58_encode(os.urandom(nbytes))


def append_event(
    root: Path,
    event_type: str,
    scan_event_payload: dict[str, Any],
    *,
    now_iso: str | None = None,
    event_id: str | None = None,
) -> str:
    """Append a single envelope row to ``<root>/.autofix/events.jsonl``.

    Parameters
    ----------
    root:
        Repository root. The events file lives at
        ``root / ".autofix" / "events.jsonl"``. The parent directory is
        created with mode 0o755 if missing.
    event_type:
        A camelCase event name from
        :data:`autofix_next.events.schema.NEW_EVENT_NAMES`. Any other value
        raises :class:`ValueError` (AC #13).
    scan_event_payload:
        Dict placed under the row's ``scan_event`` key. Typically the output
        of :meth:`autofix_next.events.schema.ScanEvent.to_payload`.
    now_iso:
        Optional pre-computed ISO-8601 timestamp. Injectable for
        deterministic tests; defaults to :func:`_now_iso_z`.
    event_id:
        Optional pre-computed event id. Injectable for deterministic tests;
        defaults to :func:`_base58_event_id`.

    Returns
    -------
    str
        The ``event_id`` actually written to the row. Useful for
        cross-referencing downstream sinks.

    Raises
    ------
    ValueError
        If ``event_type`` is not in ``NEW_EVENT_NAMES``.
    TypeError
        If ``scan_event_payload`` is not a dict (enforced for JSON-safety).
    OSError
        If the events file cannot be created or opened for append.
    """
    if event_type not in NEW_EVENT_NAMES:
        raise ValueError(
            f"unknown event name: {event_type!r}; "
            "must be one of NEW_EVENT_NAMES"
        )
    if not isinstance(scan_event_payload, dict):
        raise TypeError(
            "scan_event_payload must be a dict; "
            f"got {type(scan_event_payload).__name__}"
        )

    events_dir = Path(root) / ".autofix"
    # parents=True covers the case where ``root`` itself exists but
    # ``.autofix`` does not. exist_ok=True avoids racing with the legacy
    # writer which creates the same directory.
    events_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    events_path = events_dir / "events.jsonl"

    # If the file is missing, creating it via ``open(..., "a")`` yields mode
    # 0o666 masked by umask. Ensure mode 0o644 on first creation, mirroring
    # the legacy writer's effective permissions on default-umask systems.
    must_chmod = not events_path.exists()

    resolved_now = now_iso if now_iso is not None else _now_iso_z()
    resolved_id = event_id if event_id is not None else _base58_event_id()

    row: dict[str, Any] = {
        "event": event_type,
        "at": resolved_now,
        "event_id": resolved_id,
        "scan_event": scan_event_payload,
    }

    # ``ensure_ascii=False`` keeps non-ASCII note fields human-readable in
    # the log while remaining valid UTF-8; ``json.loads`` round-trips.
    line = json.dumps(row, ensure_ascii=False) + "\n"

    try:
        with open(events_path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
    except OSError:
        # Re-raise unchanged so callers can decide whether telemetry loss
        # should abort the scan. We do not swallow IO errors silently.
        raise

    if must_chmod:
        try:
            os.chmod(events_path, 0o644)
        except OSError:
            # On platforms where chmod is a no-op (Windows) or the file was
            # concurrently deleted, carry on — the write itself succeeded.
            pass

    return resolved_id


__all__ = [
    "append_event",
]
