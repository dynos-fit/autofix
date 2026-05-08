"""Internal watch-loop helper shared by ``autofix watch`` and
``autofix run --watch`` (ARCH-014).

The helper extracts the long-running event loop from
:mod:`autofix.cli.watch_command` so both subcommands invoke a
single implementation parameterized by a per-batch dispatcher
callable. Behavior is identical to the loop body that previously
lived inline in ``watch_command.run`` — see
:func:`run_watch_loop` for the line-by-line equivalence.

This module is internal (``_watch_loop``); it is not part of the
public ``autofix.cli`` surface and may change without
deprecation. The public surface remains the two CLI subcommands.

Deferred-import discipline
--------------------------
Like :class:`autofix.cli.watch_command.WatcherSession`, this
module MUST NOT import ``pywatchman`` at module load. The session
object is duck-typed; callers construct it (and pay the
pywatchman cost) before passing it in. A grep test
(``test_watch_loop_no_magic_numbers``) enforces both the
no-magic-numbers rule AND the no-direct-pywatchman-import rule.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from autofix.events.schema import ChangeSet


def run_watch_loop(
    session: Any,
    dispatcher: Callable[[ChangeSet], None],
    *,
    safety_sweep_seconds: int | None,
    once: bool,
) -> int:
    """Drive the Watchman event loop, dispatching once per batch.

    Parameters
    ----------
    session
        A :class:`WatcherSession`-shaped object exposing
        ``next_files()`` (returns ``(is_fresh: bool, files:
        list[str])``) and ``close()``.
    dispatcher
        A callable invoked once per Watchman batch with a
        :class:`ChangeSet` carrying the batch's files and the
        ``is_fresh_instance`` flag. The dispatcher's return
        value is discarded; exceptions are caught and logged
        to stderr (``autofix: cycle N raised <exc>``) so the
        loop continues. ``KeyboardInterrupt`` and ``SystemExit``
        are NOT caught — they propagate.
    safety_sweep_seconds
        Optional staleness threshold (seconds). When the
        wall-clock delta since the last full sweep exceeds this
        value, the next iteration forces a fresh-instance
        dispatch regardless of the Watchman event stream.
        ``None`` disables the deadline.
    once
        Test escape hatch: when ``True`` the loop dispatches at
        most one batch and returns ``0`` (or returns ``0``
        immediately when the bounded poll window expires with
        no event).

    Returns
    -------
    int
        ``0`` on clean shutdown (one-shot exit, KeyboardInterrupt,
        or SystemExit). The loop has no other clean exit path.
    """
    # Deferred import: ``time.monotonic`` is a stdlib function;
    # pywatchman is intentionally NOT imported here.
    import sys
    import time

    last_full_sweep = time.monotonic()
    processed = 0

    try:
        while True:
            is_fresh, files = session.next_files()
            now = time.monotonic()

            should_dispatch = bool(files) or is_fresh
            if (
                safety_sweep_seconds is not None
                and (now - last_full_sweep) > safety_sweep_seconds
            ):
                # Stale full-sweep deadline: force a fresh dispatch.
                is_fresh = True
                should_dispatch = True

            if should_dispatch:
                changeset = ChangeSet(
                    paths=tuple(files),
                    watcher_confidence=(
                        "full-sweep" if is_fresh else "watch"
                    ),
                    is_fresh_instance=is_fresh,
                )
                try:
                    dispatcher(changeset)
                except (KeyboardInterrupt, SystemExit):
                    # Propagate — the operator wants out.
                    raise
                except Exception as exc:
                    print(
                        f"autofix: cycle {processed} raised {exc!r}",
                        file=sys.stderr,
                        flush=True,
                    )
                if is_fresh:
                    last_full_sweep = now
                processed += 1

            if once and processed >= 1:
                return 0
            if once and not should_dispatch:
                # No event arrived within the bounded poll window.
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        session.close()


__all__ = ["run_watch_loop"]
