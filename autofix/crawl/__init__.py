"""autofix.crawl — continuous-crawl scanner subsystem.

A standalone, plug-and-play crawler subsystem. Walks a repo's
dependency graph over time, scanning **bundles** (seed + bounded-
radius neighbors) instead of singleton files. Consumed by the
autofix daemon's per-cycle orchestrator (``autofix.cli.cycle_runner``)
but can be lifted into another project unchanged — its only inputs
are two duck-typed adapters whose contracts are formalized in
``autofix.crawl.contracts``.

Subsystem map:

* ``crawl_constants`` — pinned defaults (horizons, caps, weights,
  budget tiers). Side-effect-free.
* ``contracts`` — :class:`GitLogAdapter` + :class:`CallGraphAdapter`
  Protocols. Defines what the picker and bundle expander demand
  of any injected adapter.
* ``bundles`` — :class:`Bundle` dataclass + :func:`expand_bundle`
  (BFS over the call-graph adapter, bounded by hops/files/bytes,
  with hub-saturation filtering when a ledger is supplied).
* ``score`` — freshness, relevance, priority. Pure functions.
* ``ledger`` — :class:`LedgerRow` + :class:`Ledger` (append-only
  JSONL persistence, byte-level atomic via ``O_APPEND``).
* ``picker`` — :func:`pick_next_batch` deterministic selection
  algorithm.
* ``file_classifier`` — :class:`FileClass` + classification
  helpers. Used by class-aware expansion.
* ``autofixignore`` — ``.autofixignore`` file parser
  (``pathspec``-backed).
* ``crawl_observability`` — ``CycleStats`` + ``emit_cycle_stats``
  for ``--debug-crawl`` per-cycle telemetry.

NOT included in this subsystem (and intentionally so):

* The autofix daemon's per-cycle orchestration (which knows about
  analyzers, repair, post-fix policy, PR opening). That's
  :mod:`autofix.cli.cycle_runner`.

External integrators consume the crawler via the ``__all__`` below
plus the Protocol types from ``contracts``.
"""
from __future__ import annotations

from autofix.crawl.bundles import Bundle, expand_bundle
from autofix.crawl.contracts import CallGraphAdapter, GitLogAdapter
from autofix.crawl.ledger import Ledger, LedgerRow
from autofix.crawl.picker import pick_next_batch
from autofix.crawl.score import (
    bundle_freshness,
    file_freshness,
    priority,
    relevance,
)

__all__ = [
    "Bundle",
    "CallGraphAdapter",
    "GitLogAdapter",
    "expand_bundle",
    "Ledger",
    "LedgerRow",
    "pick_next_batch",
    "file_freshness",
    "bundle_freshness",
    "relevance",
    "priority",
]
