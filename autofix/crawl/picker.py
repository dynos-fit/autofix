"""Deterministic bundle picker for the crawl (ARCH-016).

:func:`pick_next_batch` is the cycle's selection algorithm:

1. Enumerate candidate seed paths via ``git_log.list_candidate_files()``.
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
from autofix.crawl.contracts import CallGraphAdapter, GitLogAdapter
from autofix.crawl.crawl_constants import HUB_SATURATION_WINDOW_HOURS
from autofix.crawl.score import priority, relevance


def pick_next_batch(
    *,
    root: Path,
    ledger: Any,
    current_commit_sha: str,
    git_log: GitLogAdapter,
    call_graph: CallGraphAdapter,
    analyzers: list[str],
    bundles_per_cycle: int,
    now: str | None = None,
    autofixignore: Any | None = None,
    scoring_flags: Any | None = None,
    class_aware_config: Any | None = None,
    console_script_paths: Any | None = None,
) -> list[tuple[Bundle, str]]:
    """Pick this cycle's bundles + analyzer assignments.

    Returns a list of ``(Bundle, analyzer)`` pairs, length
    ``bundles_per_cycle * len(analyzers)`` (or fewer if there
    aren't enough candidate seeds in the repo).

    Optional adapter parameters:

    * ``autofixignore`` filters seed candidates and bundle
      neighbors before scoring.
    * ``scoring_flags`` (``ScoringFlags``) toggles supplemental
      scoring signals (entrypoint boost, low-value-class penalty,
      oversize-file penalty). When ``None`` or all-False, the
      ``relevance`` short-circuit produces the byte-identical
      legacy formula.
    * ``class_aware_config`` (``ClassAwareConfig``) enables
      class-aware bundle expansion (test→impl mapping, entrypoint
      multi-hop, junk-sink stop). When ``None``, ``expand_bundle``
      uses the byte-identical default BFS path.
    * ``console_script_paths`` is a frozenset of repo-relative
      :class:`Path` objects declared in ``pyproject.toml``'s
      ``[project.scripts]``. Threaded into ``classify_file`` so
      console-script entrypoints rank as ``FileClass.entrypoint``
      even when their filename doesn't match the standard
      pattern (``__main__.py``, ``manage.py``, etc).

    All four are byte-identity-safe at None — existing call sites
    that pass nothing get the legacy behavior pinned by
    ``test_picker_determinism.py``.
    """
    if not analyzers or bundles_per_cycle <= 0:
        return []

    # Candidate seed paths from git (or rglob fallback). The
    # adapter decides which file types qualify — the crawler is
    # language-agnostic at this layer.
    raw_paths = list(git_log.list_candidate_files())
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

    # Step 3: relevance per candidate. When scoring_flags is None or
    # all-False, ``relevance`` short-circuits to the legacy formula.
    # When any supplemental signal is on, we additionally pass
    # ``file_class`` (via classify_file) and ``file_size_bytes`` (via
    # stat). The expensive bits — classify_file + stat — are
    # paid only when needed.
    if scoring_flags is not None and (
        scoring_flags.entrypoint_boost
        or scoring_flags.low_value_class_penalty
        or scoring_flags.oversize_file_penalty
    ):
        from autofix.crawl.file_classifier import classify_file

        # Content-aware generated detection only adds value to the
        # low_value_class_penalty signal — we don't read content for
        # the entrypoint or oversize cases. Bound the cost: read at
        # most 8KB per candidate, errors are swallowed.
        if scoring_flags.low_value_class_penalty:
            def _read_head(p: Path) -> str:
                abs_path = p if p.is_absolute() else (root / p)
                try:
                    with abs_path.open("rb") as fh:
                        head_bytes = fh.read(8 * 1024)
                except OSError:
                    return ""
                return head_bytes.decode("utf-8", errors="replace")
            read_head: Any = _read_head
        else:
            read_head = None

        def _key(p: Path) -> float:
            cls = classify_file(
                p,
                console_script_paths=console_script_paths,
                read_head=read_head,
            )
            try:
                size = (p if p.is_absolute() else (root / p)).stat().st_size
            except OSError:
                size = 0
            return relevance(
                p, root=root, git_log=git_log,
                file_class=cls, file_size_bytes=size,
                scoring_flags=scoring_flags,
            )

        by_relevance = sorted(seed_candidates, key=_key, reverse=True)
    else:
        # Default fast-path: byte-identical to today.
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
            class_aware_config=class_aware_config,
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
