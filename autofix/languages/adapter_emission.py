"""Per-scan adapter-registration emission state (AC 6-9).

Tracks which language adapters have already emitted their
``AdapterRegistered`` event within the current scan (per contextvar
scope). Previously lived as module-level globals in ``jsts.py`` and
``go.py``; centralized here so long-running processes do not leak state
across scans.
"""
from __future__ import annotations

import os
import threading
from contextvars import ContextVar

_ADAPTER_REGISTERED_EMITTED_CV: ContextVar[set[str] | None] = ContextVar(
    "_ADAPTER_REGISTERED_EMITTED_CV", default=None
)
_LOCK = threading.Lock()


def mark_adapter_registered(name: str) -> bool:
    """Mark ``name`` as registered in the current contextvar scope.

    Returns True if this is the first emission for ``name`` in the
    current context; False if already emitted. Thread-safe: read-and-set
    is one critical section (AC 9).
    """
    with _LOCK:
        current = _ADAPTER_REGISTERED_EMITTED_CV.get()
        if current is None:
            current = set()
            _ADAPTER_REGISTERED_EMITTED_CV.set(current)
        if name in current:
            return False
        current.add(name)
        return True


def reset_adapter_emission_state() -> None:
    """Test-only: clear the current context's emission set.

    Requires ``AUTOFIX_TESTING=1`` to prevent accidental use in
    production callers (AC 7).
    """
    if os.environ.get("AUTOFIX_TESTING") != "1":
        raise RuntimeError(
            "reset_adapter_emission_state() requires AUTOFIX_TESTING=1"
        )
    with _LOCK:
        _ADAPTER_REGISTERED_EMITTED_CV.set(None)


__all__ = ["mark_adapter_registered", "reset_adapter_emission_state"]
