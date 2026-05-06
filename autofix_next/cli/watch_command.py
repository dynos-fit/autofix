"""The ``autofix-next watch`` subcommand.

Long-running Watchman-backed watcher for the autofix-next pipeline.

Design constraints (task-20260506-002 spec ACs 1-7):

* The ``pywatchman`` import is **deferred**: it happens inside
  :class:`WatcherSession`'s ``__init__``, never at module load. Importing
  this module on a host without ``pywatchman`` installed succeeds and
  produces a runnable parser; only invoking ``run()`` fails with a
  clean diagnostic (AC 3, AC 4).
* ``--safety-sweep`` accepts ``Nh``/``Nm`` duration strings via
  :func:`_safety_sweep_seconds`; bad values exit 2 with stderr substring
  ``"invalid --safety-sweep value"`` (AC 5).
* ``AUTOFIX_NEXT_WATCH_ONCE`` (truthy non-empty, not ``"0"``/``"false"``)
  causes the watcher to process exactly one event and return exit 0;
  this is the test escape hatch (AC 6).
* When a Watchman subscribe response carries ``is_fresh_instance: true``,
  the constructed :class:`ChangeSet` carries the same flag so the change
  detector triggers a bounded full sweep (AC 7).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from autofix_next.events.schema import ChangeSet

HELP_DESCRIPTION = (
    "Watchman-backed long-running scanner. Subscribes to filesystem "
    "events under --root and dispatches a scan per change batch. The "
    "Watchman daemon (and the pywatchman python bindings) must be "
    "installed; without them the subcommand fails fast at invocation "
    "time."
)

HELP_EPILOG = (
    "Test escape: set AUTOFIX_NEXT_WATCH_ONCE=1 to process one "
    "Watchman batch and exit 0."
)


_SAFETY_SWEEP_RE = re.compile(r"^([0-9]+)([hm])$")
_PYWATCHMAN_REQUIRED = (
    "pywatchman is required for `autofix-next watch`. "
    "Install with `pip install autofix-standalone[watch]` "
    "(also requires the `watchman` daemon binary)."
)


def _safety_sweep_seconds(value: str | None) -> int | None:
    """Parse an ``Nh``/``Nm`` duration string to whole seconds.

    Returns ``None`` for ``None`` input. Raises :class:`ValueError` for
    any other value that does not match ``^[0-9]+[hm]$`` (AC 5).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid --safety-sweep value: {value!r}")
    m = _SAFETY_SWEEP_RE.match(value)
    if m is None:
        raise ValueError(f"invalid --safety-sweep value: {value!r}")
    n = int(m.group(1))
    unit = m.group(2)
    return n * 60 if unit == "m" else n * 3600


def _watch_once_enabled() -> bool:
    """Return True when ``AUTOFIX_NEXT_WATCH_ONCE`` is set to a truthy
    non-empty value other than ``"0"``/``"false"`` (case-insensitive).
    """
    raw = os.environ.get("AUTOFIX_NEXT_WATCH_ONCE", "")
    if not raw:
        return False
    return raw.lower() not in {"0", "false"}


class WatcherSession:
    """Thin adapter over a ``pywatchman`` client.

    The ``pywatchman`` import is deferred to ``__init__`` so that
    importing this module never triggers a Watchman load (AC 3). When
    pywatchman is missing or its ``WatchmanError`` fires, ``__init__``
    raises :class:`RuntimeError` whose first message line starts with
    the ``"pywatchman is required"`` substring (AC 4).
    """

    def __init__(self, root: Path, since: str | None) -> None:
        try:
            import pywatchman  # noqa: PLC0415
        except (ImportError, ModuleNotFoundError) as exc:
            raise RuntimeError(_PYWATCHMAN_REQUIRED) from exc
        if pywatchman is None:
            # `monkeypatch.setitem(sys.modules, "pywatchman", None)`
            # surfaces as `None` here rather than ImportError.
            raise RuntimeError(_PYWATCHMAN_REQUIRED)

        try:
            self._client = pywatchman.client()
            self._client.query("watch-project", str(root))
        except getattr(pywatchman, "WatchmanError", Exception) as exc:
            raise RuntimeError(_PYWATCHMAN_REQUIRED) from exc

        self._root = root
        self._since = since
        self._subscribed = False
        self._sub_name = "autofix-next-watch"

    def subscribe(self) -> None:
        """Issue a Watchman subscribe query for the watched root."""
        if self._subscribed:
            return
        query: dict[str, Any] = {
            "expression": ["allof", ["type", "f"]],
            "fields": ["name"],
        }
        if self._since:
            query["since"] = self._since
        self._client.query(
            "subscribe", str(self._root), self._sub_name, query
        )
        self._subscribed = True

    def next_files(self, timeout_seconds: float = 2.0) -> tuple[bool, list[str]]:
        """Block for the next subscribe response and return the batch.

        Returns ``(is_fresh_instance, files)``. When no event arrives
        within ``timeout_seconds`` the method returns ``(False, [])``.
        """
        try:
            self._client.setTimeout(timeout_seconds)
            response = self._client.receive()
        except Exception:
            return (False, [])
        is_fresh = bool(response.get("is_fresh_instance", False))
        files = list(response.get("files") or [])
        return (is_fresh, files)

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register --root, --since, --safety-sweep on the watch parser (AC 2)."""
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help=(
            "Repo root to watch. Defaults to the current working directory "
            "at invocation time."
        ),
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help=(
            "Watchman opaque clock string (e.g. 'c:1234567890:1:0:0:0'). "
            "When omitted, the watcher subscribes from now."
        ),
    )
    parser.add_argument(
        "--safety-sweep",
        type=str,
        default=None,
        dest="safety_sweep",
        help=(
            "Maximum tolerated staleness for the last full sweep, expressed "
            "as Nh or Nm (e.g. '1h', '30m'). When the wall-clock delta "
            "since the last full sweep exceeds this, the watcher forces a "
            "full sweep regardless of the Watchman event stream."
        ),
    )


def run(args: argparse.Namespace, *, root: Path | None = None) -> int:
    """Entry point for the `autofix-next watch` subcommand.

    Returns the process exit code: 0 on clean shutdown (one-shot test
    mode or KeyboardInterrupt), 2 on argparse-level errors (bad
    --safety-sweep, missing pywatchman).
    """
    # AC 5: --safety-sweep format check up front.
    try:
        safety_sweep_seconds = _safety_sweep_seconds(getattr(args, "safety_sweep", None))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # AC 2 (Implicit Requirement): resolve --root at run time, not parse time.
    effective_root: Path = (
        args.root if args.root is not None
        else (root if root is not None else Path.cwd())
    )

    # AC 3 + AC 4: deferred pywatchman load via WatcherSession.__init__.
    try:
        session = WatcherSession(effective_root, getattr(args, "since", None))
    except RuntimeError as exc:
        msg = str(exc)
        # AC 4: stderr must contain the exact substring; no traceback.
        print(msg, file=sys.stderr)
        return 2

    try:
        session.subscribe()
    except Exception as exc:
        print(f"{_PYWATCHMAN_REQUIRED} ({exc})", file=sys.stderr)
        session.close()
        return 2

    once = _watch_once_enabled()
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
                # Stale full-sweep deadline: force a fresh-instance dispatch.
                is_fresh = True
                should_dispatch = True

            if should_dispatch:
                changeset = ChangeSet(
                    paths=tuple(files),
                    watcher_confidence=("full-sweep" if is_fresh else "watch"),
                    is_fresh_instance=is_fresh,
                )
                _dispatch_scan(effective_root, changeset)
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


def _dispatch_scan(root: Path, changeset: ChangeSet) -> None:
    """Run a single scan against ``changeset``. Tests patch
    ``autofix_next.funnel.pipeline.run_scan``."""
    from autofix_next.funnel.pipeline import run_scan  # local import — avoid

    scan_id = f"watch-{int(time.time())}"
    run_scan(root, changeset, scan_id=scan_id)
