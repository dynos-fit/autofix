"""autofix.crawl — continuous-crawl scanner subsystem (ARCH-016).

The crawl is the operator-facing default of bare ``autofix``: a
long-running, cost-aware daemon that walks the repo dependency
graph over time, scanning **bundles** (seed + bounded-radius
neighbors) instead of singleton files.

Subsystem map:

* ``crawl_constants`` — pinned defaults (horizons, caps, weights,
  budget tiers). Side-effect-free.
* ``bundles`` — :class:`Bundle` dataclass + :func:`expand_bundle`
  (BFS over the call graph, bounded by hops/files/bytes, with
  hub-saturation filtering when a ledger is supplied).
* ``score`` — freshness, relevance, priority. Pure functions.
* ``ledger`` — :class:`LedgerRow` + :class:`Ledger` (append-only
  JSONL persistence, byte-level atomic via ``O_APPEND``).
* ``picker`` — :func:`pick_next_batch` deterministic selection
  algorithm.
* ``driver`` — :func:`run_crawl_once` and
  :func:`run_crawl_continuously` (added in PR-2).

The ``run_crawl_once`` / ``run_crawl_continuously`` driver is
written in PR-2; the names appear in ``__all__`` here so the
package's public surface is stable across the two PRs.
"""
from __future__ import annotations

from autofix.crawl.bundles import Bundle, expand_bundle
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
    "expand_bundle",
    "Ledger",
    "LedgerRow",
    "pick_next_batch",
    "file_freshness",
    "bundle_freshness",
    "relevance",
    "priority",
]
