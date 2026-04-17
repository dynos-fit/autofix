"""CLI-side event ingress: build the initial ``ScanStarted`` payload.

The ingress layer is the boundary where a concrete invocation (CLI today;
future: git hook, file-watcher, CI runner) is translated into a neutral
:class:`~autofix_next.events.schema.ScanEvent`. No side effects occur here:
the caller persists the returned event via
:func:`autofix_next.telemetry.events_log.append_event`. This keeps the
ingress unit-testable without a writable filesystem and makes it trivial
to fan additional sources in later without growing the telemetry writer's
API surface.
"""

from __future__ import annotations

from pathlib import Path

from autofix_next.events.schema import ScanEvent


def ingest_cli_invocation(
    root: Path,
    full_sweep: bool,
    scan_id: str,
    *,
    commit_sha: str | None = None,
    base_sha: str | None = None,
    watcher_confidence: str = "diff-head1",
) -> ScanEvent:
    """Translate a CLI invocation into a ``ScanStarted`` :class:`ScanEvent`.

    Parameters
    ----------
    root:
        Repository root passed via ``--root``. Only its ``name`` is used
        here (as the ``repo_id``); the full path remains the CLI's
        responsibility.
    full_sweep:
        Preserved on the returned event's ``extra`` so replay tooling
        can reconstruct the exact CLI flags used.
    scan_id:
        The scan identifier threaded through every subsequent envelope
        row for this run; the caller generates it once and passes it in.
    commit_sha, base_sha:
        Optional SHAs for the scan window. ``None`` is a valid sentinel
        meaning "not recorded at this layer" — the change-detector can
        populate them in a richer second event if needed.
    watcher_confidence:
        One of ``"full-sweep"``, ``"full-sweep-fallback"``, or
        ``"diff-head1"`` (see :func:`autofix_next.events.change_detector.detect`).
        Defaults to the diff-based label because that is the CLI's
        default path.

    Returns
    -------
    ScanEvent
        A ``ScanStarted`` event whose ``source`` is ``"cli"``. The caller
        is responsible for persisting it via
        :func:`autofix_next.telemetry.events_log.append_event`.
    """
    if not isinstance(root, Path):
        raise TypeError(
            f"root must be a pathlib.Path; got {type(root).__name__}"
        )
    if not isinstance(scan_id, str) or not scan_id:
        raise ValueError("scan_id must be a non-empty string")

    return ScanEvent(
        event_type="ScanStarted",
        repo_id=root.name,
        commit_sha=commit_sha,
        base_sha=base_sha,
        watcher_confidence=watcher_confidence,
        source="cli",
        scan_id=scan_id,
        extra={"full_sweep": bool(full_sweep)},
    )


__all__ = ["ingest_cli_invocation"]
