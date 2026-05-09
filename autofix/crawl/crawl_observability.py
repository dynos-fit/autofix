"""Per-cycle observability for the crawl driver (ARCH-016 / task-20260508-002 AC 21).

Single emission entry point :func:`emit_cycle_stats` consumes a
:class:`CycleStats` snapshot and writes one of three output shapes:

* ``quiet=True``               → nothing
* ``debug_crawl=True``         → multi-line breakdown of every counter
* otherwise                    → single-line INFO summary

The dataclass is intentionally **mutable** so the driver can increment
counters in-place over the course of a cycle without re-allocating a
frozen instance per update. There is no IO in this module aside from
the explicit ``file=`` write inside ``emit_cycle_stats``.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import IO


@dataclass
class CycleStats:
    """Mutable per-cycle counter bag for the crawl driver.

    Default values are zero / empty so a fresh ``CycleStats()`` reads
    correctly even if the cycle aborts before populating any field.
    The driver mutates the fields in place; nothing about this class
    requires immutability.
    """

    top_seeds: list[str] = field(default_factory=list)
    bundles_built: int = 0
    bundle_size_bytes_list: list[int] = field(default_factory=list)
    budget_hits_files: int = 0
    budget_hits_bytes: int = 0
    budget_hits_hops: int = 0
    junk_sinks_dropped: int = 0
    skipped_by_autofixignore: int = 0
    skipped_by_class: int = 0
    skipped_by_size_cap: int = 0


def _format_bytes_list(values: list[int]) -> str:
    """Render a list of byte sizes as ``min/median/max`` for the
    debug output. Returns ``"-"`` for an empty list rather than 0/0/0
    so operators can tell "no bundles built" apart from "all bundles
    were 0 bytes".
    """
    if not values:
        return "-"
    sorted_v = sorted(values)
    n = len(sorted_v)
    median = sorted_v[n // 2] if n % 2 == 1 else (sorted_v[n // 2 - 1] + sorted_v[n // 2]) // 2
    return f"min={sorted_v[0]} median={median} max={sorted_v[-1]}"


def _format_top_seeds(seeds: list[str], cap: int = 5) -> str:
    """Render the top-N seed list. ``cap`` keeps single-line output bounded."""
    if not seeds:
        return "-"
    head = seeds[:cap]
    suffix = f" (+{len(seeds) - cap} more)" if len(seeds) > cap else ""
    return ", ".join(head) + suffix


def emit_cycle_stats(
    stats: CycleStats,
    *,
    quiet: bool,
    debug_crawl: bool,
    file: IO[str] = sys.stderr,
) -> None:
    """Emit ``stats`` to ``file`` (default stderr).

    Three modes:

    * ``quiet=True``   — emits nothing. ``debug_crawl`` is ignored under
      quiet (quiet wins; this matches the existing ``--quiet`` semantics
      in the rest of the CLI).
    * ``debug_crawl=True`` (and not quiet) — multi-line breakdown of
      every counter on the dataclass.
    * otherwise — single-line INFO summary suitable for tailing logs.

    The function never raises for any input shape: a write failure on
    ``file`` is swallowed so a broken pipe (e.g. ``| head``) cannot
    abort the crawl cycle.
    """
    if quiet:
        return

    if debug_crawl:
        lines = [
            "autofix: cycle stats (debug):",
            f"  bundles_built={stats.bundles_built}",
            f"  bundle_sizes_bytes: {_format_bytes_list(stats.bundle_size_bytes_list)}",
            f"  top_seeds: {_format_top_seeds(stats.top_seeds, cap=10)}",
            f"  budget_hits: files={stats.budget_hits_files} "
            f"bytes={stats.budget_hits_bytes} hops={stats.budget_hits_hops}",
            f"  junk_sinks_dropped={stats.junk_sinks_dropped}",
            f"  skipped_by_autofixignore={stats.skipped_by_autofixignore}",
            f"  skipped_by_class={stats.skipped_by_class}",
            f"  skipped_by_size_cap={stats.skipped_by_size_cap}",
        ]
        payload = "\n".join(lines) + "\n"
    else:
        payload = (
            f"autofix: cycle stats: bundles={stats.bundles_built} "
            f"junk_sinks_dropped={stats.junk_sinks_dropped} "
            f"skipped_by_autofixignore={stats.skipped_by_autofixignore} "
            f"budget_hits={stats.budget_hits_files + stats.budget_hits_bytes + stats.budget_hits_hops}\n"
        )

    try:
        file.write(payload)
        file.flush()
    except (OSError, ValueError):
        # ValueError covers "I/O operation on closed file" without
        # spamming the caller with a traceback; OSError covers EPIPE
        # and friends. Observability is best-effort.
        return


__all__ = [
    "CycleStats",
    "emit_cycle_stats",
]
