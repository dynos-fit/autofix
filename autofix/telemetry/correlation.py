"""Per-scan correlation IDs as contextvars (AC 6, 7)."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_EVENT_ID: ContextVar[str | None] = ContextVar("_EVENT_ID", default=None)
_COMMIT_SHA: ContextVar[str | None] = ContextVar("_COMMIT_SHA", default=None)
_SCAN_ID: ContextVar[str | None] = ContextVar("_SCAN_ID", default=None)


def current_event_id() -> str | None:
    """Return the current scan's event id, or None if unset."""
    return _EVENT_ID.get()


def current_commit_sha() -> str | None:
    """Return the current scan's commit SHA, or None if unset."""
    return _COMMIT_SHA.get()


def current_scan_id() -> str | None:
    """Return the current scan id, or None if unset."""
    return _SCAN_ID.get()


@contextmanager
def set_event_id(event_id: str) -> Iterator[None]:
    """Set the current event id for the duration of the with-block."""
    token = _EVENT_ID.set(event_id)
    try:
        yield
    finally:
        _EVENT_ID.reset(token)


@contextmanager
def set_commit_sha(sha: str) -> Iterator[None]:
    """Set the current commit SHA for the duration of the with-block."""
    token = _COMMIT_SHA.set(sha)
    try:
        yield
    finally:
        _COMMIT_SHA.reset(token)


@contextmanager
def set_scan_id(scan_id: str) -> Iterator[None]:
    """Set the current scan id for the duration of the with-block."""
    token = _SCAN_ID.set(scan_id)
    try:
        yield
    finally:
        _SCAN_ID.reset(token)


__all__ = [
    "current_event_id",
    "current_commit_sha",
    "current_scan_id",
    "set_event_id",
    "set_commit_sha",
    "set_scan_id",
]
