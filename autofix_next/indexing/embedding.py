"""Per-symbol embedding sidecar — HNSW ANN over SymbolRecord stream.

Opt-in semantic recall layer maintained incrementally between scans.
Reuses the sentence-transformers/all-MiniLM-L6-v2 (384-dim) + hnswlib
stack from :mod:`autofix_next.dedup.embedding` — zero new runtime deps.
When the policy flag is off (default), every public method is a no-op
and no ML model is loaded.

Public surface (AC 1)
---------------------
* :data:`EMBEDDING_SIDECAR_MODEL_NAME` — frozen model id.
* :data:`EMBEDDING_SIDECAR_DIM`        — vector dimension (384).
* :class:`SymbolRecall`                — frozen slotted recall hit.
* :class:`EmbeddingSidecar`            — stateful per-scan index owner.
* :func:`probe_embedding_sidecar`      — non-raising capability probe.

On-disk layout (AC 9 / AC 33)
-----------------------------
* ``<root>/.autofix-next/indexing/embedding.hnswlib.idx``    — binary index.
* ``<root>/.autofix-next/indexing/embedding-metadata.json``  — schema sidecar.

Metadata schema
---------------
``{"schema_version": "embedding_sidecar_v1", "model_name": ...,
"dim": 384, "next_label": int, "labels": {label: {...}}}``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from autofix_next.telemetry.atomic import atomic_write_json
from autofix_next.telemetry.events_log import append_event

EMBEDDING_SIDECAR_MODEL_NAME: str = "all-MiniLM-L6-v2"
EMBEDDING_SIDECAR_DIM: int = 384

_SCHEMA_VERSION: str = "embedding_sidecar_v1"
_SIDECAR_DIR: str = ".autofix-next/indexing"
_INDEX_FILENAME: str = "embedding.hnswlib.idx"
_METADATA_FILENAME: str = "embedding-metadata.json"


# ----------------------------------------------------------------------
# Dataclasses
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SymbolRecall:
    """A single recall hit from :meth:`EmbeddingSidecar.recall`.

    Field order is pinned by AC 2: ``symbol_id``, ``similarity``,
    ``symbol_name``, ``language``, ``path``.
    """

    symbol_id: str
    similarity: float
    symbol_name: str
    language: str
    path: str


# ----------------------------------------------------------------------
# Event emission helpers
# ----------------------------------------------------------------------


def emit_incremental_update_event(
    root: Path,
    *,
    symbols_added: int,
    symbols_updated: int,
    duration_ms: int,
) -> None:
    """Emit a single ``EmbeddingSidecarIncrementalUpdate`` row (AC 20).

    The payload is pinned to exactly five keys: ``event_type``,
    ``repo_id``, ``symbols_added``, ``symbols_updated``, ``duration_ms``.
    OSError is swallowed so telemetry loss never aborts a scan — mirrors
    the ``append_event`` contract used by :meth:`EmbeddingSidecar.cold_rebuild`.
    """
    try:
        append_event(
            root,
            "EmbeddingSidecarIncrementalUpdate",
            {
                "event_type": "EmbeddingSidecarIncrementalUpdate",
                "repo_id": str(root),
                "symbols_added": int(symbols_added),
                "symbols_updated": int(symbols_updated),
                "duration_ms": int(duration_ms),
            },
        )
    except OSError:
        pass


# ----------------------------------------------------------------------
# Probe
# ----------------------------------------------------------------------


def probe_embedding_sidecar(policy: dict | None = None) -> tuple[bool, str]:
    """Return ``(enabled, reason)`` for the embedding sidecar (AC 13).

    Does not instantiate :class:`EmbeddingSidecar`. ``reason`` values:

    * ``"disabled_by_policy"`` — flag absent or ``enabled=False``.
    * ``"ok"``                 — flag true and imports succeed.
    * ``"deps_missing"``       — flag true but deps not importable.
    """

    enabled = False
    if isinstance(policy, dict):
        index_cfg = policy.get("index")
        if isinstance(index_cfg, dict):
            sidecar_cfg = index_cfg.get("embedding_sidecar")
            if isinstance(sidecar_cfg, dict):
                enabled = bool(sidecar_cfg.get("enabled", False))
    if not enabled:
        return (False, "disabled_by_policy")
    try:
        import sentence_transformers  # noqa: F401
        import hnswlib  # noqa: F401
    except ImportError:
        return (False, "deps_missing")
    return (True, "ok")


# ----------------------------------------------------------------------
# EmbeddingSidecar
# ----------------------------------------------------------------------


class EmbeddingSidecar:
    """Per-scan-root HNSW sidecar over a symbol-level embedding stream.

    Strictly opt-in. When ``policy["index"]["embedding_sidecar"]["enabled"]``
    is not true, the sidecar is a behavioral no-op: no ML model is loaded,
    no HNSW index allocated, no on-disk state touched. A disabled sidecar
    returns empty / falsy defaults from every public method (AC 5).

    When enabled but the optional deps are unimportable, ``__init__``
    catches the :class:`ImportError`, emits one
    ``EmbeddingSidecarDegraded`` event, and self-disables (AC 6). No raise.
    """

    def __init__(self, root: Path, policy: dict | None = None) -> None:
        self._root: Path = Path(root)
        self._policy: dict | None = policy
        self._enabled: bool = False
        self._hnsw: Any = None
        self._model: Any = None
        self._labels: dict[int, dict[str, str]] = {}
        self._symbol_to_label: dict[str, int] = {}
        self._deleted_labels: set[int] = set()
        self._next_label: int = 0
        # task-012 AC 20 incremental-update counters reset on every flush.
        self._added_since_flush: int = 0
        self._updated_since_flush: int = 0
        self._incremental_start: float = time.perf_counter()

        enabled_flag = False
        if isinstance(policy, dict):
            index_cfg = policy.get("index")
            if isinstance(index_cfg, dict):
                sidecar_cfg = index_cfg.get("embedding_sidecar")
                if isinstance(sidecar_cfg, dict):
                    enabled_flag = bool(sidecar_cfg.get("enabled", False))

        if not enabled_flag:
            return

        try:
            import hnswlib  # type: ignore[import-not-found]
            import sentence_transformers  # type: ignore[import-not-found]  # noqa: F401
        except ImportError as exc:
            try:
                append_event(
                    self._root,
                    "EmbeddingSidecarDegraded",
                    {
                        "event_type": "EmbeddingSidecarDegraded",
                        "repo_id": str(self._root),
                        "reason": str(exc),
                    },
                )
            except OSError:
                pass
            return

        self._hnsw = hnswlib.Index(
            space="cosine", dim=EMBEDDING_SIDECAR_DIM
        )
        self._hnsw.init_index(
            max_elements=10000, ef_construction=200, M=16
        )
        self._hnsw.set_ef(50)
        self._enabled = True

    # -- properties --------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Return whether the sidecar is active (AC 4)."""

        return self._enabled

    @property
    def symbol_count(self) -> int:
        """Return the live (non-deleted) symbol count (AC 12)."""

        if not self._enabled:
            return 0
        return len(self._symbol_to_label)

    # -- mutation ----------------------------------------------------

    def upsert_symbol(
        self, symbol_id: str, text: str, metadata: dict
    ) -> None:
        """Insert or replace a symbol's vector (AC 7).

        When the sidecar is disabled, the call is a no-op. On replace, the
        previous label is marked deleted in the HNSW index and the symbol
        is re-added under a fresh label (hnswlib's replacement pattern).
        """

        if not self._enabled:
            return

        from autofix_next.dedup.embedding import embed_text

        vec = embed_text(text)

        previous_label = self._symbol_to_label.get(symbol_id)
        if previous_label is not None:
            try:
                self._hnsw.mark_deleted(previous_label)
            except RuntimeError:
                # Already deleted or label out of range — either way, the
                # downstream add supersedes it logically.
                pass
            self._deleted_labels.add(previous_label)
            self._labels.pop(previous_label, None)
            self._updated_since_flush += 1
        else:
            self._added_since_flush += 1

        label = self._next_label
        self._next_label += 1
        self._ensure_capacity()
        self._hnsw.add_items([vec], [label])
        self._labels[label] = {
            "symbol_id": symbol_id,
            "symbol_name": str(metadata.get("symbol_name", symbol_id)),
            "language": str(metadata.get("language", "python")),
            "path": str(metadata.get("path", "")),
        }
        self._symbol_to_label[symbol_id] = label

    def _ensure_capacity(self) -> None:
        """Grow the HNSW backing store when we exhaust the current slab."""

        current_max = self._hnsw.get_max_elements()
        if self._next_label >= current_max:
            self._hnsw.resize_index(current_max * 2)

    # -- recall ------------------------------------------------------

    def recall(
        self,
        query_text: str,
        top_k: int = 5,
        threshold: float = 0.75,
    ) -> list[SymbolRecall]:
        """Return similar symbols above ``threshold``, sorted desc (AC 8)."""

        if not self._enabled:
            return []
        if not self._symbol_to_label:
            return []

        from autofix_next.dedup.embedding import embed_text

        vec = embed_text(query_text)
        effective_k = min(top_k, len(self._symbol_to_label))
        if effective_k <= 0:
            return []

        labels, distances = self._hnsw.knn_query([vec], k=effective_k)

        # hnswlib.knn_query returns results in ascending-distance order.
        # ``similarity = 1 - distance`` is therefore in descending order
        # already — the subsequent filter only drops entries, so no
        # explicit sort is required (perf-004).
        hits: list[SymbolRecall] = []
        for label, dist in zip(labels[0], distances[0]):
            label_int = int(label)
            if label_int in self._deleted_labels:
                continue
            meta = self._labels.get(label_int)
            if meta is None:
                continue
            similarity = 1.0 - float(dist)
            if similarity < threshold:
                continue
            hits.append(
                SymbolRecall(
                    symbol_id=meta["symbol_id"],
                    similarity=similarity,
                    symbol_name=meta["symbol_name"],
                    language=meta["language"],
                    path=meta["path"],
                )
            )
        return hits

    # -- persistence -------------------------------------------------

    def _sidecar_dir(self) -> Path:
        return self._root / _SIDECAR_DIR

    def save(self) -> bool:
        """Atomically persist the index + metadata (AC 9)."""

        if not self._enabled or self.symbol_count == 0:
            return False
        try:
            out_dir = self._sidecar_dir()
            out_dir.mkdir(parents=True, exist_ok=True)
            self._hnsw.save_index(str(out_dir / _INDEX_FILENAME))
        except OSError:
            return False

        metadata = {
            "schema_version": _SCHEMA_VERSION,
            "model_name": EMBEDDING_SIDECAR_MODEL_NAME,
            "dim": EMBEDDING_SIDECAR_DIM,
            "next_label": self._next_label,
            "labels": {
                str(k): v for k, v in self._labels.items()
            },
            "symbol_to_label": {
                k: v for k, v in self._symbol_to_label.items()
            },
            "deleted_labels": sorted(int(x) for x in self._deleted_labels),
        }
        return atomic_write_json(
            self._sidecar_dir() / _METADATA_FILENAME, metadata
        )

    def load(self) -> bool:
        """Load index + metadata; return False on any schema mismatch (AC 10)."""

        if not self._enabled:
            return False
        index_path = self._sidecar_dir() / _INDEX_FILENAME
        meta_path = self._sidecar_dir() / _METADATA_FILENAME
        if not index_path.exists() or not meta_path.exists():
            return False

        import json

        try:
            raw = meta_path.read_text(encoding="utf-8")
            meta = json.loads(raw)
        except (OSError, ValueError):
            return False

        if (
            not isinstance(meta, dict)
            or meta.get("schema_version") != _SCHEMA_VERSION
            or meta.get("model_name") != EMBEDDING_SIDECAR_MODEL_NAME
            or meta.get("dim") != EMBEDDING_SIDECAR_DIM
        ):
            return False

        try:
            next_label = int(meta.get("next_label", 0))
            max_elements = max(next_label * 2, 10000)
            self._hnsw.load_index(
                str(index_path), max_elements=max_elements
            )
        except (OSError, RuntimeError):
            return False

        self._labels = {
            int(k): dict(v) for k, v in meta.get("labels", {}).items()
        }
        self._symbol_to_label = dict(meta.get("symbol_to_label", {}))
        self._deleted_labels = {
            int(x) for x in meta.get("deleted_labels", [])
        }
        self._next_label = next_label
        return True

    # -- incremental update emission --------------------------------

    def flush_incremental_update(
        self,
        *,
        duration_ms: int | None = None,
    ) -> bool:
        """Emit ``EmbeddingSidecarIncrementalUpdate`` (task-012 AC 20).

        Emits when the sidecar is enabled AND at least one add or update
        has accumulated since the last flush / construction. The payload
        carries exactly five keys: ``event_type``, ``repo_id``,
        ``symbols_added``, ``symbols_updated``, ``duration_ms``.

        ``duration_ms`` defaults to the wall-clock delta since the
        previous flush (or sidecar construction). Callers that already
        measured the batch latency can pass it explicitly — useful for
        deterministic tests.

        Returns ``True`` iff an event was written. Counters and the
        per-batch start timestamp reset in both emit and no-emit
        branches so a subsequent batch is measured independently.
        """

        if not self._enabled:
            return False
        added = self._added_since_flush
        updated = self._updated_since_flush
        if added == 0 and updated == 0:
            # Still reset the start timestamp so a follow-up batch is
            # measured from "now" rather than the sidecar's init.
            self._incremental_start = time.perf_counter()
            return False
        if duration_ms is None:
            resolved_ms = int(
                (time.perf_counter() - self._incremental_start) * 1000
            )
        else:
            resolved_ms = int(duration_ms)
        emit_incremental_update_event(
            self._root,
            symbols_added=added,
            symbols_updated=updated,
            duration_ms=resolved_ms,
        )
        # perf-002: persist incremental state after emitting the batch
        # event so the next scan can ``load()`` instead of paying the
        # cold-rebuild cost again. ``save()`` is a best-effort — a
        # returned ``False`` here does not block the event-emission
        # side effect, which is the spec-critical contract.
        self.save()
        # perf-003: deleted_labels is unbounded across long-running
        # processes. Trigger a compaction pass when it exceeds a soft
        # threshold — compaction rewrites the live subset into a fresh
        # HNSW and drops the dead labels. 1024 is a cheap safety net;
        # at steady state the set only accumulates replaced symbols.
        if len(self._deleted_labels) > 1024:
            self._compact()
        self._added_since_flush = 0
        self._updated_since_flush = 0
        self._incremental_start = time.perf_counter()
        return True

    def _compact(self) -> None:
        """Rebuild the HNSW index in place, dropping deleted labels (perf-003).

        Triggered by :meth:`flush_incremental_update` when the dead-label
        count crosses a soft threshold. The live metadata is preserved
        (labels are re-assigned 0..N-1 so ``next_label`` resets cleanly)
        and the new index carries the same symbol_ids.
        """
        if not self._enabled or not self._hnsw:
            return
        try:
            import hnswlib  # type: ignore[import-not-found]
        except ImportError:
            return
        live = [
            (sid, label)
            for sid, label in self._symbol_to_label.items()
            if label not in self._deleted_labels
        ]
        if not live:
            return
        try:
            live_vectors = [
                self._hnsw.get_items([lbl])[0] for _, lbl in live
            ]
        except (RuntimeError, IndexError):
            return
        new_index = hnswlib.Index(space="cosine", dim=EMBEDDING_SIDECAR_DIM)
        new_index.init_index(
            max_elements=max(len(live) * 2, 10000),
            ef_construction=200,
            M=16,
        )
        new_index.set_ef(50)
        new_labels: dict[int, dict[str, str]] = {}
        new_symbol_to_label: dict[str, int] = {}
        for new_label, ((sid, old_label), vec) in enumerate(zip(live, live_vectors)):
            new_index.add_items([vec], [new_label])
            meta = self._labels.get(old_label)
            if meta is not None:
                new_labels[new_label] = dict(meta)
            new_symbol_to_label[sid] = new_label
        self._hnsw = new_index
        self._labels = new_labels
        self._symbol_to_label = new_symbol_to_label
        self._deleted_labels = set()
        self._next_label = len(live)

    # -- cold rebuild -----------------------------------------------

    def cold_rebuild(self, symbol_stream: Iterable[dict]) -> int:
        """Fresh-build the index from a stream of symbol dicts (AC 11)."""

        if not self._enabled:
            return 0

        import hnswlib  # type: ignore[import-not-found]

        # Materialize once so we can size the HNSW backing store up-front
        # and avoid the resize storm (perf-001). A list is required here
        # — hnswlib does not expose a streaming mode, and cold rebuild is
        # a bounded-once-per-repo operation.
        symbols = list(symbol_stream)
        max_elements = max(len(symbols) * 2, 10000)

        self._hnsw = hnswlib.Index(
            space="cosine", dim=EMBEDDING_SIDECAR_DIM
        )
        self._hnsw.init_index(
            max_elements=max_elements, ef_construction=200, M=16
        )
        self._hnsw.set_ef(50)
        self._labels = {}
        self._symbol_to_label = {}
        self._deleted_labels = set()
        self._next_label = 0
        # Cold rebuild is a replacement, not an increment — counters
        # reset here so the next flush does not spuriously report the
        # rebuild batch as an "incremental update."
        self._added_since_flush = 0
        self._updated_since_flush = 0

        start = time.perf_counter()
        count = 0
        for symbol in symbols:
            symbol_id = str(symbol.get("symbol_id", ""))
            text = str(symbol.get("text", ""))
            if not symbol_id or not text:
                continue
            metadata = {
                "symbol_name": symbol.get("symbol_name", symbol_id),
                "language": symbol.get("language", "python"),
                "path": symbol.get("path", ""),
            }
            self.upsert_symbol(symbol_id, text, metadata)
            count += 1
        duration_ms = int((time.perf_counter() - start) * 1000)

        if count > 0:
            self.save()

        try:
            append_event(
                self._root,
                "EmbeddingSidecarColdRebuild",
                {
                    "event_type": "EmbeddingSidecarColdRebuild",
                    "repo_id": str(self._root),
                    "symbol_count": count,
                    "duration_ms": duration_ms,
                    "model_name": EMBEDDING_SIDECAR_MODEL_NAME,
                },
            )
        except OSError:
            pass

        return count


__all__ = [
    "EMBEDDING_SIDECAR_DIM",
    "EMBEDDING_SIDECAR_MODEL_NAME",
    "EmbeddingSidecar",
    "SymbolRecall",
    "probe_embedding_sidecar",
]
