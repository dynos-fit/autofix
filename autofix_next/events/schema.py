"""Typed event payloads and the frozen NEW_EVENT_NAMES set.

AC #13 pins the exact set of camelCase event names emitted by autofix_next.
These must never collide with the legacy snake_case names written by
``autofix/runtime/dynos.py:log_event``. Any new event added in a later
segment must extend ``NEW_EVENT_NAMES`` here and the matching ``EventType``
literal in lockstep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# -- Event name enum (AC #13) ------------------------------------------------

EventType = Literal[
    "ScanStarted",
    "SymbolIndexed",
    "EvidencePacketBuilt",
    "LLMCallGated",
    "SARIFEmitted",
    "ScanCompleted",
    "InvalidationComputed",
    "PriorityScored",
    "FindingDeduped",
    "DedupEmbeddingTierStatus",
    "ClusterStorePersisted",
    "AdapterRegistered",
    "AdapterPrecisionUnavailable",
    "LanguageShardPersisted",
]

NEW_EVENT_NAMES: frozenset[str] = frozenset(
    {
        "ScanStarted",
        "SymbolIndexed",
        "EvidencePacketBuilt",
        "LLMCallGated",
        "SARIFEmitted",
        "ScanCompleted",
        "InvalidationComputed",
        "PriorityScored",
        "FindingDeduped",
        "DedupEmbeddingTierStatus",
        "ClusterStorePersisted",
        "AdapterRegistered",
        "AdapterPrecisionUnavailable",
        "LanguageShardPersisted",
    }
)


# -- ScanEvent --------------------------------------------------------------


@dataclass(slots=True)
class ScanEvent:
    """A single row of the events.jsonl stream.

    ``to_payload`` returns the dict placed under the ``scan_event`` key of an
    events.jsonl row. The outer row (with ``timestamp`` etc.) is the
    responsibility of the telemetry writer in a later segment.
    """

    event_type: str
    repo_id: str
    commit_sha: str | None
    base_sha: str | None
    watcher_confidence: str
    source: str
    scan_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """Return the dict that goes into the ``scan_event`` key of a row.

        Keys with ``None`` values are preserved so downstream consumers can
        distinguish "missing" from "absent". The ``extra`` mapping is merged
        as a nested dict to keep the top-level shape stable.
        """
        payload: dict[str, Any] = {
            "event_type": self.event_type,
            "repo_id": self.repo_id,
            "commit_sha": self.commit_sha,
            "base_sha": self.base_sha,
            "watcher_confidence": self.watcher_confidence,
            "source": self.source,
            "scan_id": self.scan_id,
            "extra": dict(self.extra),
        }
        return payload


# -- ChangeSet --------------------------------------------------------------


@dataclass(slots=True)
class ChangeSet:
    """An immutable set of repo-relative paths touched in a scan window.

    ``paths`` is coerced to a tuple in ``__post_init__`` so the dataclass is
    hashable and safe to use as a cache key. Callers may pass any iterable
    (list, tuple, generator).
    """

    paths: tuple[str, ...]
    watcher_confidence: str
    is_fresh_instance: bool = False

    def __post_init__(self) -> None:
        # Accept any iterable at construction time; normalize to tuple.
        if not isinstance(self.paths, tuple):
            if isinstance(self.paths, (str, bytes)):
                # Reject accidental single-string pass-through: it would be
                # iterated character-by-character otherwise.
                raise TypeError(
                    "ChangeSet.paths must be an iterable of strings, "
                    f"not {type(self.paths).__name__}"
                )
            # Validate each element is a string before coercing.
            coerced: list[str] = []
            for p in self.paths:  # type: ignore[assignment]
                if not isinstance(p, str):
                    raise TypeError(
                        f"ChangeSet.paths contains non-str element: {p!r}"
                    )
                coerced.append(p)
            object.__setattr__(self, "paths", tuple(coerced))
        if not isinstance(self.watcher_confidence, str):
            raise TypeError(
                "ChangeSet.watcher_confidence must be a string"
            )
        if not isinstance(self.is_fresh_instance, bool):
            raise TypeError(
                "ChangeSet.is_fresh_instance must be a bool"
            )


__all__ = [
    "EventType",
    "NEW_EVENT_NAMES",
    "ScanEvent",
    "ChangeSet",
]
