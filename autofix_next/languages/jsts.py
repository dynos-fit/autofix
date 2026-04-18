"""JS/TS language adapter (task-006 AC #12, #13, #14).

Responsibilities
----------------
* Probe ``tree_sitter`` + ``tree_sitter_typescript`` at module import
  time. The ``available`` bool on every :class:`JSTSAdapter` instance
  reflects the outcome so the scheduler can route around a missing
  grammar without re-probing on every parse.
* Emit an ``AdapterRegistered`` envelope row on first use when
  ``available == False`` so operators can see why the adapter is
  degraded.
* Implement :meth:`JSTSAdapter.scip_index` — invoke the
  ``scip-typescript`` binary resolved via
  :func:`autofix_next.languages.bin_cache.ensure_binary`, persist the
  output under
  ``<repo_root>/.autofix-next/state/index/scip-ts/<hash>.scip`` using
  the same 4-step atomic-rename + ``flock`` pattern as
  :mod:`autofix_next.languages.bin_cache`.
* Emit ``LanguageShardPersisted`` on every successful persist and
  ``AdapterPrecisionUnavailable`` on subprocess non-zero exit /
  binary-unavailable.

Telemetry helpers are defined inline rather than imported so this module
stays self-contained — the funnel pipeline does the same thing
(``_emit_invalidation_computed_event``). Every helper swallows ``OSError``
on the events.jsonl write: telemetry loss must never abort the scan.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autofix_next.languages import bin_cache, register


# Grammar probe — performed once at module import time. The two names
# (``tree_sitter``, ``tree_sitter_typescript``) must both resolve for
# cheap parsing to be available. Failure is cached on the module rather
# than re-probed per instance so scheduler fan-out does not re-pay the
# ImportError cost on every ``.ts`` file.
# Grammar imports go through importlib to hide ``import tree_sitter`` from
# the legacy AST-based import-graph builder (autofix.platform.build_import_graph),
# which stem-matches the bare ``import tree_sitter`` against the repo-local
# ``autofix_next/parsing/tree_sitter.py`` shim and produces a false-positive
# edge. Mirrors the workaround in autofix_next/languages/python.py.
import importlib as _importlib

try:
    _ts_mod = _importlib.import_module("tree_sitter")  # type: ignore[assignment]
    _tsts_mod = _importlib.import_module("tree_sitter_typescript")  # type: ignore[assignment]
    _GRAMMAR_OK: bool = True
except ImportError:
    _ts_mod = None  # type: ignore[assignment]
    _tsts_mod = None  # type: ignore[assignment]
    _GRAMMAR_OK = False


# ---------------------------------------------------------------------------
# ParseResult-shaped payload for cheap path when grammar is absent.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _EmptyParseResult:
    """ParseResult-shaped sentinel returned by :meth:`parse_cheap` when
    the tree-sitter-typescript grammar is unavailable (AC #12).

    Mirrors the duck-typed surface of
    :class:`autofix_next.languages.python.ParseResult` so downstream
    side-effect-only consumers can iterate without raising. The ``tree``
    attribute is deliberately ``None`` — callers MUST guard for it
    before descending into tree-sitter APIs.
    """

    source_bytes: bytes
    tree: Any = None
    path: Path | None = None
    relpath: str = "<memory>"
    lines: list[str] | None = None


# ---------------------------------------------------------------------------
# Telemetry helpers (inline). Pattern mirrors
# ``autofix_next.funnel.pipeline._emit_invalidation_computed_event``.
# ---------------------------------------------------------------------------


# Module-level one-shot flag: ``AdapterRegistered`` is emitted at most
# once per adapter per process on first use (AC #12).
_ADAPTER_REGISTERED_EMITTED: bool = False


def _emit_adapter_registered(
    repo_root: Path,
    *,
    language: str,
    extensions: tuple[str, ...],
    available: bool,
    reason: str,
) -> None:
    """Emit one ``AdapterRegistered`` envelope row on first use.

    OSError-swallowing matches the funnel pipeline's telemetry helpers —
    a lost row must never abort the scan.
    """

    global _ADAPTER_REGISTERED_EMITTED
    if _ADAPTER_REGISTERED_EMITTED:
        return
    _ADAPTER_REGISTERED_EMITTED = True
    try:
        from autofix_next.telemetry import events_log

        events_log.append_event(
            repo_root,
            "AdapterRegistered",
            {
                "event_type": "AdapterRegistered",
                "repo_id": repo_root.name,
                "language": language,
                "extensions": list(extensions),
                "available": available,
                "reason": reason,
            },
        )
    except OSError:
        pass


def _emit_adapter_precision_unavailable(
    repo_root: Path,
    *,
    language: str,
    reason: str,
) -> None:
    """Emit one ``AdapterPrecisionUnavailable`` envelope row (AC #13)."""

    try:
        from autofix_next.telemetry import events_log

        events_log.append_event(
            repo_root,
            "AdapterPrecisionUnavailable",
            {
                "event_type": "AdapterPrecisionUnavailable",
                "repo_id": repo_root.name,
                "language": language,
                "reason": reason,
            },
        )
    except OSError:
        pass


def _emit_language_shard_persisted(
    repo_root: Path,
    *,
    language: str,
    shard_path: str,
    cache_mode: str,
    module_root_or_none: str | None,
) -> None:
    """Emit one ``LanguageShardPersisted`` envelope row (AC #14)."""

    try:
        from autofix_next.telemetry import events_log

        events_log.append_event(
            repo_root,
            "LanguageShardPersisted",
            {
                "event_type": "LanguageShardPersisted",
                "repo_id": repo_root.name,
                "language": language,
                "shard_path": shard_path,
                "cache_mode": cache_mode,
                "module_root_or_none": module_root_or_none,
            },
        )
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Flock helpers — mirror ``bin_cache._acquire_lock`` verbatim so the
# scip-ts shard persist path observes the same concurrency contract as
# the binary cache.
# ---------------------------------------------------------------------------

_LOCK_TIMEOUT_SECONDS: float = 30.0
_LOCK_INITIAL_BACKOFF: float = 0.05
_LOCK_MAX_BACKOFF: float = 1.0


def _acquire_shard_lock(lock_path: Path):
    """Context-manager-style flock helper for the scip-ts shard dir.

    Duplicated from :func:`autofix_next.languages.bin_cache._acquire_lock`
    (duplication accepted — the two modules have independent cache
    locations and the alternative is a shared flock helper for a 30-line
    routine).
    """
    from contextlib import contextmanager

    @contextmanager
    def _cm():
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
            backoff = _LOCK_INITIAL_BACKOFF
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(min(backoff, max(0.0, deadline - time.monotonic())))
                    backoff = min(backoff * 2, _LOCK_MAX_BACKOFF)
                except OSError as exc:
                    if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                        if time.monotonic() >= deadline:
                            raise BlockingIOError(
                                f"flock timeout after {_LOCK_TIMEOUT_SECONDS}s"
                            ) from exc
                        time.sleep(
                            min(backoff, max(0.0, deadline - time.monotonic()))
                        )
                        backoff = min(backoff * 2, _LOCK_MAX_BACKOFF)
                        continue
                    raise
            try:
                yield fd
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    return _cm()


def _atomic_rename(tmp: Path, final: Path, state_dir: Path) -> None:
    """4-step atomic rename: fsync tmp → fsync parent → replace → fsync parent.

    Mirrors :func:`autofix_next.languages.bin_cache._atomic_install`
    minus the ``chmod +x`` step (a SCIP index file is data, not an
    executable).
    """
    # Step 1: fsync tmp file contents.
    fd = os.open(str(tmp), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

    # Steps 2 + 3 + 4: fsync parent dir, atomic replace, fsync parent dir.
    try:
        parent_fd = os.open(str(state_dir), os.O_RDONLY)
    except OSError:
        # Some filesystems (tmpfs, certain test envs) reject dir fsync.
        # ``os.replace`` is atomic wrt readers regardless.
        os.replace(str(tmp), str(final))
        return

    try:
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
        os.replace(str(tmp), str(final))
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# The adapter.
# ---------------------------------------------------------------------------


class JSTSAdapter:
    """``LanguageAdapter`` implementation for TypeScript / JavaScript.

    ``available`` is set at instance construction time from the module
    level probe (AC #12). When False, :meth:`parse_cheap` returns an
    empty-tree ParseResult-shaped object rather than raising, and a
    single ``AdapterRegistered`` envelope row (``available=False``,
    ``reason="grammar_missing"``) is emitted on first use.

    Precision for JS/TS is delivered via :meth:`scip_index`, not
    :meth:`parse_precise` — the latter returns ``None`` today.
    """

    language: str = "typescript"
    extensions: tuple[str, ...] = (".ts", ".tsx", ".js", ".jsx")

    def __init__(self) -> None:
        # Re-probe at construction time so tests that monkeypatch
        # ``builtins.__import__`` to simulate a missing grammar observe
        # the ``available == False`` branch on a fresh instance (the
        # module-level probe is a one-time snapshot — the probe is
        # cheap enough to repeat).
        try:
            _importlib.import_module("tree_sitter")  # probe only
            _importlib.import_module("tree_sitter_typescript")  # probe only

            self.available: bool = True
        except ImportError:
            self.available = False

    # ------------------------------------------------------------------
    # Cheap parse path.
    # ------------------------------------------------------------------

    def parse_cheap(self, source: bytes) -> Any:
        """Return a ParseResult-shaped object (AC #12).

        When the grammar is unavailable, returns an
        :class:`_EmptyParseResult` carrying the original ``source`` bytes
        so downstream callers can introspect without re-reading. One
        ``AdapterRegistered`` envelope row is emitted on first use.
        """

        if not self.available:
            # Emit AdapterRegistered on first use. ``repo_root`` is not
            # known at this call site — use the cwd as a best-effort
            # stand-in; the funnel pipeline passes the real root when
            # it drives the adapter.
            _emit_adapter_registered(
                Path.cwd(),
                language=self.language,
                extensions=self.extensions,
                available=False,
                reason="grammar_missing",
            )
            return _EmptyParseResult(source_bytes=source)

        # Grammar is present — but the cheap tree-sitter-typescript
        # wiring is not part of task-006's scope (seg-plan defers real
        # parsing to a later task). Return an empty-tree ParseResult so
        # the scheduler sees a conforming shape.
        return _EmptyParseResult(source_bytes=source)

    # ------------------------------------------------------------------
    # Precision parse path (returns None per AC #12 derived contract).
    # ------------------------------------------------------------------

    def parse_precise(self, source: bytes) -> Any | None:
        """No tree-sitter precision pass for JS/TS — precision is
        delivered via :meth:`scip_index`. Returns ``None``.
        """

        return None

    # ------------------------------------------------------------------
    # Protocol conformance stubs (symbol_kind / signature).
    # ------------------------------------------------------------------

    def symbol_kind(self, node: Any) -> str:
        """Classify a parse-tree node. JS/TS deferred — returns ``"unknown"``."""

        return "unknown"

    def signature(self, node: Any) -> str:
        """Short signature for a parse-tree node. JS/TS deferred — empty."""

        return ""

    # ------------------------------------------------------------------
    # SCIP index (AC #13, AC #14).
    # ------------------------------------------------------------------

    def scip_index(self, workdir: Path) -> Path | None:
        """Invoke ``scip-typescript index`` and persist the output.

        The binary is resolved via
        :func:`autofix_next.languages.bin_cache.ensure_binary`. On a
        non-zero subprocess exit or a
        :class:`bin_cache.BinaryUnavailableError`, emits an
        ``AdapterPrecisionUnavailable`` envelope row and returns
        ``None``.

        :class:`bin_cache.BinaryIntegrityError` is deliberately NOT
        caught — an integrity failure aborts the scan per the
        bin_cache exception contract (AC #29).
        """

        workdir = Path(workdir)
        # Adapter-level contract for this task: workdir IS the repo root
        # (the scan orchestrator invokes one adapter per repo).
        repo_root = workdir

        if not self.available:
            _emit_adapter_precision_unavailable(
                repo_root,
                language=self.language,
                reason="grammar_missing",
            )
            return None

        try:
            bin_path = bin_cache.ensure_binary("scip-typescript")
        except bin_cache.BinaryUnavailableError:
            # Recoverable: degrade to cheap-path. Integrity errors are
            # NOT caught — they propagate to abort the scan.
            _emit_adapter_precision_unavailable(
                repo_root,
                language=self.language,
                reason="binary_download_failed",
            )
            return None

        state_dir = repo_root / ".autofix-next" / "state" / "index" / "scip-ts"
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            _emit_adapter_precision_unavailable(
                repo_root,
                language=self.language,
                reason="binary_nonzero_exit",
            )
            return None

        content_hash = hashlib.sha256(
            str(workdir.resolve()).encode("utf-8")
        ).hexdigest()[:16]
        final = state_dir / f"{content_hash}.scip"
        tmp = state_dir / f"{content_hash}.scip.tmp"
        lock_path = state_dir / ".scip-ts-lock"

        # Under the shard dir lock so concurrent scans cannot collide on
        # the same ``<hash>.scip.tmp`` path.
        try:
            with _acquire_shard_lock(lock_path):
                # Cache fast-path: a concurrent process may have already
                # persisted the shard while we waited for the lock. If
                # ``final`` exists, treat this as a cache hit and emit
                # ``LanguageShardPersisted`` with ``cache_mode="reused"``.
                if final.exists():
                    _emit_language_shard_persisted(
                        repo_root,
                        language=self.language,
                        shard_path=str(final),
                        cache_mode="reused",
                        module_root_or_none=None,
                    )
                    return final

                try:
                    result = subprocess.run(
                        [str(bin_path), "index", "--output", str(tmp)],
                        cwd=str(workdir),
                        timeout=600,
                        capture_output=True,
                    )
                except (subprocess.TimeoutExpired, OSError):
                    _emit_adapter_precision_unavailable(
                        repo_root,
                        language=self.language,
                        reason="binary_nonzero_exit",
                    )
                    try:
                        tmp.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
                    return None

                if result.returncode != 0:
                    _emit_adapter_precision_unavailable(
                        repo_root,
                        language=self.language,
                        reason="binary_nonzero_exit",
                    )
                    try:
                        tmp.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
                    return None

                try:
                    _atomic_rename(tmp, final, state_dir)
                except OSError:
                    _emit_adapter_precision_unavailable(
                        repo_root,
                        language=self.language,
                        reason="binary_nonzero_exit",
                    )
                    try:
                        tmp.unlink()
                    except FileNotFoundError:
                        pass
                    except OSError:
                        pass
                    return None

                _emit_language_shard_persisted(
                    repo_root,
                    language=self.language,
                    shard_path=str(final),
                    cache_mode="fresh",
                    module_root_or_none=None,
                )
                return final
        except (BlockingIOError, OSError):
            _emit_adapter_precision_unavailable(
                repo_root,
                language=self.language,
                reason="binary_nonzero_exit",
            )
            return None


# ---------------------------------------------------------------------------
# Self-registration (AC #5).
# ---------------------------------------------------------------------------

register(JSTSAdapter())


__all__ = ["JSTSAdapter"]
