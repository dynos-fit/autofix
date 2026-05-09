"""Deterministic bundle picker for the crawl (ARCH-016).

:func:`pick_next_batch` is the cycle's selection algorithm:

1. Enumerate candidate seed paths via ``git_log.list_python_files()``.
2. (Optional) drop seeds matched by ``autofixignore`` before any
   relevance work — saves cycles when the picker would otherwise
   compute scores for ignored files.
3. Compute :func:`relevance` for each candidate.
4. Take the top ``bundles_per_cycle * 3`` candidates (over-pick,
   then narrow after expansion — gives the picker headroom in case
   some bundles are dropped to saturation).
5. Expand each candidate into a :class:`Bundle` via
   :func:`expand_bundle` (with the ledger, to honor saturation).
6. Compute :func:`priority` per bundle. Sort descending.
7. Take the top ``bundles_per_cycle`` bundles.
8. Emit one ``(bundle, analyzer)`` pair per analyzer in the
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
    autofixignore: Any | None = None,
) -> list[tuple[Bundle, str]]:
    """Pick this cycle's bundles + analyzer assignments.

    Returns a list of ``(Bundle, analyzer)`` pairs, length
    ``bundles_per_cycle * len(analyzers)`` (or fewer if there
    aren't enough candidate seeds in the repo).

    Optional ``autofixignore`` filters seed candidates before the
    relevance sort. Existing call sites that pass nothing get
    byte-identical behavior; the determinism test pins this.
    """
    if not analyzers or bundles_per_cycle <= 0:
        return []

    # Candidate seed paths from git (or rglob fallback).
    raw_paths = list(git_log.list_python_files())
    seed_candidates: list[Path] = [
        Path(p) if not isinstance(p, Path) else p
        for p in raw_paths
    ]

    # Step 2: optional autofixignore filter on seed candidates.
    # Resolves seed to absolute under ``root`` for the matcher so
    # relative seed strings emitted by git adapters work the same as
    # absolute paths emitted by rglob fallback.
    if autofixignore is not None:
        filtered: list[Path] = []
        for p in seed_candidates:
            seed_abs = p if p.is_absolute() else (root / p)
            if autofixignore.matches(seed_abs, root):
                continue
            filtered.append(p)
        seed_candidates = filtered

    # Step 3: relevance per candidate.
    by_relevance = sorted(
        seed_candidates,
        key=lambda p: relevance(p, root=root, git_log=git_log),
        reverse=True,
    )

    # Step 4: over-pick to give priority sort headroom.
    over_pick = max(bundles_per_cycle * 3, bundles_per_cycle)
    top_seeds = by_relevance[:over_pick]

    # Step 5: expand each into a Bundle.
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
            autofixignore=autofixignore,
        )
        if bundle.fingerprint in seen_fingerprints:
            continue
        seen_fingerprints.add(bundle.fingerprint)
        expansions.append(bundle)

    # Step 6: priority sort.
    expansions.sort(
        key=lambda b: priority(
            b, ledger, current_commit_sha,
            root=root, git_log=git_log,
        ),
        reverse=True,
    )

    # Step 7: cap at bundles_per_cycle.
    chosen = expansions[:bundles_per_cycle]

    # Step 8: emit (bundle, analyzer) pairs.
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
            tzinfo=timezone.utc,
        )
    start = end - timedelta(hours=HUB_SATURATION_WINDOW_HOURS)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = ["pick_next_batch"]
