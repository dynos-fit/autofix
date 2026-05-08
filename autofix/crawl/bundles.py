"""Bundle primitive: seed + bounded-radius neighbors (ARCH-016).

A :class:`Bundle` is a connected subgraph of files in the
dependency graph: a designated **seed** plus 1-hop neighbors
(callers + callees), bounded by three independent caps —
``max_hops``, ``max_files``, ``max_bytes`` — whichever trips
first.

The expansion uses an injected ``call_graph`` adapter (duck-typed
against :class:`autofix.invalidation.call_graph.CallGraph` —
specifically the ``neighbors_of(path) -> list[Path]`` method). A
:class:`autofix.crawl.ledger.Ledger` may be supplied to the
expander; when present, neighbors whose
``bundle_appearance_count_in_window`` exceeds
:data:`MAX_HUB_APPEARANCES` are filtered out (hub saturation).
The seed itself is NEVER filtered, only neighbors.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autofix.crawl.crawl_constants import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_FILES,
    MAX_BUNDLE_HOPS,
    MAX_HUB_APPEARANCES,
)


@dataclass(frozen=True)
class Bundle:
    """An immutable subgraph snapshot — seed + selected neighbors."""

    seed_path: Path
    file_paths: tuple[Path, ...]
    total_bytes: int
    fingerprint: str

    @staticmethod
    def compute_fingerprint(file_paths: tuple[Path, ...]) -> str:
        """Canonical SHA-256 of the sorted POSIX file-path list.

        Same file set → same fingerprint regardless of order or seed
        identity. This makes the (fingerprint, analyzer) cache key
        meaningful across cycles.
        """
        canonical = sorted(str(p) for p in file_paths)
        payload = json.dumps(canonical, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def expand_bundle(
    *,
    seed_path: Path,
    root: Path,
    call_graph: Any,
    ledger: Any | None = None,
    max_hops: int = MAX_BUNDLE_HOPS,
    max_files: int = MAX_BUNDLE_FILES,
    max_bytes: int = MAX_BUNDLE_BYTES,
    window_start: str | None = None,
    now: str | None = None,
) -> Bundle:
    """BFS from ``seed_path``, bounded by 3 caps; produce a Bundle.

    Parameters
    ----------
    seed_path
        The bundle's anchor file. Always included regardless of any
        saturation state.
    root
        Repository root — used to resolve file sizes for the
        ``max_bytes`` budget.
    call_graph
        Duck-typed adapter exposing ``neighbors_of(path) ->
        list[Path]``. Returns the union of callers + callees for the
        given file.
    ledger
        Optional. When supplied, neighbors with appearance count
        ``>= MAX_HUB_APPEARANCES`` in the saturation window are
        dropped. Seeds are never dropped.
    max_hops, max_files, max_bytes
        Bundle expansion caps. Independent — whichever trips first
        wins. Defaults come from :mod:`autofix.crawl.crawl_constants`.
    window_start, now
        ISO 8601 strings defining the saturation window when
        ``ledger`` is supplied. Ignored otherwise.

    Returns
    -------
    Bundle
        Immutable snapshot. Seed always first in ``file_paths``;
        neighbor order is BFS visitation order (which is
        deterministic given the call_graph adapter).
    """
    seed_size = _safe_size(seed_path)
    selected: list[Path] = [seed_path]
    total_bytes = seed_size

    if max_hops <= 0:
        return _build_bundle(seed_path, selected, total_bytes)

    # 1-hop neighbors (callers + callees union from the graph).
    neighbors = list(call_graph.neighbors_of(seed_path))

    for cand in neighbors:
        if cand == seed_path:
            continue
        if len(selected) >= max_files:
            break

        # Hub saturation: drop neighbors that are over-represented in
        # the recent window. Seeds are never filtered (only neighbors).
        if ledger is not None and window_start is not None:
            now_value = now if now is not None else _utcnow_iso_z()
            count = ledger.bundle_appearance_count_in_window(
                cand, window_start, now_value
            )
            if count >= MAX_HUB_APPEARANCES:
                continue

        cand_size = _safe_size(cand)
        if total_bytes + cand_size > max_bytes:
            continue

        selected.append(cand)
        total_bytes += cand_size

    return _build_bundle(seed_path, selected, total_bytes)


def _build_bundle(seed: Path, files: list[Path], total_bytes: int) -> Bundle:
    file_paths = tuple(files)
    return Bundle(
        seed_path=seed,
        file_paths=file_paths,
        total_bytes=total_bytes,
        fingerprint=Bundle.compute_fingerprint(file_paths),
    )


def _safe_size(path: Path) -> int:
    """Return file size in bytes; on stat failure, return 0 (safe)."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _utcnow_iso_z() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["Bundle", "expand_bundle"]
