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

Class-aware expansion (opt-in):
    Pass a :class:`ClassAwareConfig` to enable file-classifier-driven
    expansion. When the seed is a ``test`` file the mirror impl path
    (resolved via :func:`map_test_to_impl`) is prioritized first. When
    the seed is ``config``, hops are hard-capped at 1 and non-source
    neighbors are dropped. When the seed is ``entrypoint``, BFS uses
    :data:`MAX_BUNDLE_HOPS_ENTRYPOINT` hops. Junk-sink classes
    (``generated``/``vendor``/``cache``/``build_output``/``lockfile``/
    ``binary``) are dropped at every step before bytes-budget.

Filter ordering (when class-aware is on):
    1. hub saturation
    2. junk-sink class filter
    3. autofixignore filter
    4. config-seed non-source filter
    5. bytes-budget cap

When ``class_aware_config`` and ``autofixignore`` are both ``None``
behavior is byte-identical to the pre-flag implementation.
"""
from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from autofix.crawl.contracts import CallGraphAdapter
from autofix.crawl.crawl_constants import (
    CLASS_EXPANSION_PRIORITY,
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_FILES,
    MAX_BUNDLE_HOPS,
    MAX_BUNDLE_HOPS_ENTRYPOINT,
    MAX_HUB_APPEARANCES,
)
from autofix.crawl.file_classifier import FileClass, classify_file, map_test_to_impl


# Classes that are dropped from bundle expansion when class-aware is
# active. Mirrors the spec's "junk-sink stop" list.
_JUNK_SINK_CLASSES: frozenset[FileClass] = frozenset({
    FileClass.generated,
    FileClass.vendor,
    FileClass.cache,
    FileClass.build_output,
    FileClass.lockfile,
    FileClass.binary,
})


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


@dataclass(frozen=True)
class ClassAwareConfig:
    """Opt-in configuration for class-aware bundle expansion.

    When passed to :func:`expand_bundle` (non-``None``) the expander
    consults the file classifier to:

    * route test seeds to their mirror impl file first;
    * cap config seeds at 1 hop and drop non-source neighbors;
    * use :data:`MAX_BUNDLE_HOPS_ENTRYPOINT` for entrypoint seeds;
    * drop junk-sink class neighbors before bytes-budget.

    Construction must be additive (default kwargs) so callers that
    only know they want "class-aware on" can ``ClassAwareConfig(root=…)``.
    """

    root: Path


def expand_bundle(
    *,
    seed_path: Path,
    root: Path,
    call_graph: CallGraphAdapter,
    ledger: Any | None = None,
    max_hops: int = MAX_BUNDLE_HOPS,
    max_files: int = MAX_BUNDLE_FILES,
    max_bytes: int = MAX_BUNDLE_BYTES,
    window_start: str | None = None,
    now: str | None = None,
    class_aware_config: ClassAwareConfig | None = None,
    autofixignore: Any | None = None,
) -> Bundle:
    """BFS from ``seed_path``, bounded by 3 caps; produce a Bundle.

    Parameters
    ----------
    seed_path
        The bundle's anchor file. Always included regardless of any
        saturation state, autofixignore match, or class filter.
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
    class_aware_config
        Optional :class:`ClassAwareConfig`. ``None`` (default) keeps
        the legacy 1-hop expansion. When non-None, class-aware logic
        kicks in (see module docstring).
    autofixignore
        Optional :class:`autofix.crawl.autofixignore.AutofixIgnore`.
        When non-None, neighbor candidates whose path matches the
        ignore are dropped. Seeds are NEVER dropped by this filter.

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

    if class_aware_config is None:
        # ----- Legacy 1-hop path. When ``autofixignore`` is also
        # ----- None this branch is byte-identical to the pre-flag
        # ----- implementation. The default-path test pins this.
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
                    cand, window_start, now_value,
                )
                if count >= MAX_HUB_APPEARANCES:
                    continue

            # Autofixignore filter (additive — seeds never excluded).
            if autofixignore is not None and autofixignore.matches(cand, root):
                continue

            cand_size = _safe_size(cand)
            if total_bytes + cand_size > max_bytes:
                continue

            selected.append(cand)
            total_bytes += cand_size

        return _build_bundle(seed_path, selected, total_bytes)

    # ----- Class-aware path. -----
    return _expand_bundle_class_aware(
        seed_path=seed_path,
        root=root,
        call_graph=call_graph,
        ledger=ledger,
        max_files=max_files,
        max_bytes=max_bytes,
        window_start=window_start,
        now=now,
        class_aware_config=class_aware_config,
        autofixignore=autofixignore,
        seed_size=seed_size,
        selected=selected,
        total_bytes=total_bytes,
    )


def _expand_bundle_class_aware(
    *,
    seed_path: Path,
    root: Path,
    call_graph: CallGraphAdapter,
    ledger: Any | None,
    max_files: int,
    max_bytes: int,
    window_start: str | None,
    now: str | None,
    class_aware_config: ClassAwareConfig,
    autofixignore: Any | None,
    seed_size: int,  # noqa: ARG001 — kept for symmetry; total_bytes already includes
    selected: list[Path],
    total_bytes: int,
) -> Bundle:
    """Class-aware BFS implementation. See module docstring for the
    filter ordering contract."""
    # Resolve the seed's class once. Used to route the rest of the
    # algorithm.
    seed_class = classify_file(seed_path)

    # Per-class hop budget.
    if seed_class is FileClass.entrypoint:
        hop_budget = MAX_BUNDLE_HOPS_ENTRYPOINT
    elif seed_class is FileClass.config:
        # AC 8: config seeds are hard-capped at 1 hop.
        hop_budget = 1
    else:
        hop_budget = MAX_BUNDLE_HOPS

    if hop_budget <= 0:
        return _build_bundle(seed_path, selected, total_bytes)

    # When the seed is a test file with a present mirror impl, prepend
    # the impl path to the priority list so it surfaces first in the
    # bundle (after the seed itself).
    forced_first: list[Path] = []
    if seed_class is FileClass.test:
        impl = map_test_to_impl(seed_path, class_aware_config.root)
        if impl is not None and impl != seed_path:
            forced_first.append(impl)

    # BFS with explicit visited set (deterministic since the call_graph
    # adapter returns a deterministic neighbor list).
    visited: set[Path] = {seed_path}
    queue: deque[tuple[Path, int]] = deque()

    # Seed the queue with hop-1 candidates. The forced-first list is
    # synthetic (the impl mirror may not be a literal neighbor of the
    # test seed). We still subject it to all filters.
    hop1_seeds: list[Path] = list(forced_first)
    for n in call_graph.neighbors_of(seed_path):
        if n in hop1_seeds:
            continue
        hop1_seeds.append(n)

    for n in hop1_seeds:
        if n == seed_path or n in visited:
            continue
        visited.add(n)
        queue.append((n, 1))

    now_value: Optional[str] = None  # lazy — only computed if ledger is set

    while queue:
        if len(selected) >= max_files:
            break

        cand, depth = queue.popleft()

        # ---- Filter ordering (CRITICAL — see module docstring).
        # 1) Hub saturation. Drops regardless of class.
        if ledger is not None and window_start is not None:
            if now_value is None:
                now_value = now if now is not None else _utcnow_iso_z()
            try:
                count = ledger.bundle_appearance_count_in_window(
                    cand, window_start, now_value,
                )
            except Exception:  # noqa: BLE001
                count = 0
            if count >= MAX_HUB_APPEARANCES:
                continue

        # 2) Junk-sink class filter.
        cand_class = classify_file(cand)
        if cand_class in _JUNK_SINK_CLASSES:
            continue

        # 3) Autofixignore filter (additive — seeds never excluded).
        if autofixignore is not None and autofixignore.matches(cand, root):
            continue

        # 4) Config-seed: keep only ``source`` neighbors.
        if seed_class is FileClass.config and cand_class is not FileClass.source:
            continue

        # 5) Bytes-budget — last, only computed for survivors.
        cand_size = _safe_size(cand)
        if total_bytes + cand_size > max_bytes:
            continue

        selected.append(cand)
        total_bytes += cand_size

        # Enqueue next-hop neighbors when the budget allows.
        if depth < hop_budget:
            try:
                next_hop = call_graph.neighbors_of(cand)
            except Exception:  # noqa: BLE001
                next_hop = []
            for nxt in next_hop:
                if nxt == seed_path or nxt in visited:
                    continue
                visited.add(nxt)
                queue.append((nxt, depth + 1))

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


__all__ = ["Bundle", "ClassAwareConfig", "expand_bundle"]


# ``CLASS_EXPANSION_PRIORITY`` is imported above so consumer modules
# referencing it from this module's namespace work; the inline body
# does not consult it directly because the BFS is breadth-first by
# hop and uses preserved neighbor order. Reference here so the linter
# doesn't flag the import as unused.
_ = CLASS_EXPANSION_PRIORITY
