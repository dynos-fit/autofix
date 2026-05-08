"""Deterministic bundle picker for the crawl (ARCH-016).

:func:`pick_next_batch` is the cycle's selection algorithm:

1. Enumerate candidate seed paths via ``git_log.list_python_files()``.
2. Compute :func:`relevance` for each candidate.
3. Take the top ``bundles_per_cycle * 3`` candidates (over-pick,
   then narrow after expansion — gives the picker headroom in case
   some bundles are dropped to saturation).
4. Expand each candidate into a :class:`Bundle` via
   :func:`expand_bundle` (with the ledger, to honor saturation).
5. Compute :func:`priority` per bundle. Sort descending.
6. Take the top ``bundles_per_cycle`` bundles.
7. Emit one ``(bundle, analyzer)`` pair per analyzer in the
   resolved set.

Determinism: given identical inputs, the algorithm produces the
same bundle list in the same order. Verified by
``test_picker_determinism.py``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from autofix.crawl.bundles import Bundle, expand_bundle
from autofix.crawl.crawl_constants import HUB_SATURATION_WINDOW_HOURS
from autofix.crawl.score import priority, relevance


def pick_next_batch(
    *,
    root: Path,
    ledger: Any,
    current_commit_sha: str,
    git_log: Any,
    call_graph: Any,
    analyzers: list[str],
    bundles_per_cycle: int,
    now: str | None = None,
) -> list[tuple[Bundle, str]]:
    """Pick this cycle's bundles + analyzer assignments.

    Returns a list of ``(Bundle, analyzer)`` pairs, length
    ``bundles_per_cycle * len(analyzers)`` (or fewer if there
    aren't enough candidate seeds in the repo).
    """
    if not analyzers or bundles_per_cycle <= 0:
        return []

    # Candidate seed paths from git (or rglob fallback).
    raw_paths = list(git_log.list_python_files())
    seed_candidates = [
        Path(p) if not isinstance(p, Path) else p
        for p in raw_paths
    ]

    # Step 2: relevance per candidate.
    by_relevance = sorted(
        seed_candidates,
        key=lambda p: relevance(p, root=root, git_log=git_log),
        reverse=True,
    )

    # Step 3: over-pick to give priority sort headroom.
    over_pick = max(bundles_per_cycle * 3, bundles_per_cycle)
    top_seeds = by_relevance[:over_pick]

    # Step 4: expand each into a Bundle.
    window_start = _window_start_iso(now)
    expansions: list[Bundle] = []
    seen_fingerprints: set[str] = set()
    for seed in top_seeds:
        # Resolve to absolute path under root if not already.
        seed_abs = seed if seed.is_absolute() else (root / seed)
        bundle = expand_bundle(
            seed_path=seed_abs,
            root=root,
            call_graph=call_graph,
            ledger=ledger,
            window_start=window_start,
            now=now,
        )
        if bundle.fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(bundle.fingerprint)
        expansions.append(bundle)

    # Step 5: priority sort.
    expansions.sort(
        key=lambda b: priority(
            b, ledger, current_commit_sha,
            root=root, git_log=git_log,
        ),
        reverse=True,
    )

    # Step 6: cap at bundles_per_cycle.
    chosen = expansions[:bundles_per_cycle]

    # Step 7: emit (bundle, analyzer) pairs.
    out: list[tuple[Bundle, str]] = []
    for bundle in chosen:
        for analyzer in analyzers:
            out.append((bundle, analyzer))
    return out


def _window_start_iso(now: str | None) -> str:
    """Compute the start of the saturation window."""
    from datetime import datetime, timedelta, timezone

    if now is None:
        end = datetime.now(timezone.utc)
    else:
        end = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    start = end - timedelta(hours=HUB_SATURATION_WINDOW_HOURS)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["pick_next_batch"]
