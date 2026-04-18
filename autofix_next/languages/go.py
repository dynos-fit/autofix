"""Go language adapter (task-006 AC #15..#21).

Responsibilities
----------------
* Probe ``tree_sitter`` + ``tree_sitter_go`` at module import time. The
  ``available`` bool on every :class:`GoAdapter` instance reflects the
  outcome so the scheduler can route around a missing grammar without
  re-probing on every parse.
* Implement :meth:`GoAdapter.scip_index` — group the input list of
  changed ``.go`` paths by their nearest-ancestor ``go.mod`` directory,
  then invoke ``scip-go --module <module_root> --output <shard>.scip``
  exactly ONCE per distinct module root. Per-module cache keys are
  ``sha256(module_path + "|" + sha256(go.mod) + "|" + sha256(go.sum or b""))``
  and each shard is persisted under
  ``<repo_root>/.autofix-next/state/index/scip-go/<cache-key>.scip``
  using the 4-step atomic-rename + ``flock`` pattern borrowed from
  :mod:`autofix_next.languages.bin_cache`.
* Emit ``LanguageShardPersisted`` (``cache_mode="fresh"`` or
  ``"reused"``) on every shard touch and
  ``AdapterPrecisionUnavailable`` on a subprocess non-zero exit /
  missing binary.

``bin_cache.BinaryIntegrityError`` is deliberately NOT caught — an
integrity failure aborts the scan per the bin_cache exception contract
(AC #29).

Telemetry helpers are defined inline rather than imported so this module
stays self-contained, mirroring :mod:`autofix_next.languages.jsts`.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autofix_next.languages import bin_cache, register


_log = logging.getLogger(__name__)


# Grammar probe — performed once at module import time. The two names
# (``tree_sitter``, ``tree_sitter_go``) must both resolve for cheap
# parsing to be considered available. Failure is cached on the module
# rather than re-probed per instance so scheduler fan-out does not re-pay
# the ImportError cost on every ``.go`` file.
# Grammar imports go through importlib to hide ``import tree_sitter`` from
# the legacy AST-based import-graph builder (autofix.platform.build_import_graph),
# which stem-matches the bare ``import tree_sitter`` against the repo-local
# ``autofix_next/parsing/tree_sitter.py`` shim and produces a false-positive
# edge. Mirrors the workaround in autofix_next/languages/python.py.
import importlib as _importlib

try:
    _ts_mod = _importlib.import_module("tree_sitter")  # type: ignore[assignment]
    _tsg_mod = _importlib.import_module("tree_sitter_go")  # type: ignore[assignment]
    _GRAMMAR_OK: bool = True
except ImportError:
    _ts_mod = None  # type: ignore[assignment]
    _tsg_mod = None  # type: ignore[assignment]
    _GRAMMAR_OK = False


# ---------------------------------------------------------------------------
# ParseResult-shaped payload for cheap path when the grammar is absent.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _EmptyParseResult:
    """ParseResult-shaped sentinel returned by :meth:`parse_cheap` when
    the tree-sitter-go grammar is unavailable (AC #15).

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
# once per adapter per process on first use.
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
    """Emit one ``AdapterPrecisionUnavailable`` envelope row."""

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
    """Emit one ``LanguageShardPersisted`` envelope row (AC #17, #20)."""

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
# Flock + atomic-rename helpers — mirror ``jsts._acquire_shard_lock`` /
# ``jsts._atomic_rename`` so the scip-go shard persist path observes the
# same concurrency contract as the scip-ts shard and the binary cache.
# ---------------------------------------------------------------------------

_LOCK_TIMEOUT_SECONDS: float = 30.0
_LOCK_INITIAL_BACKOFF: float = 0.05
_LOCK_MAX_BACKOFF: float = 1.0


def _acquire_shard_lock(lock_path: Path):
    """Context-manager-style flock helper for the scip-go shard dir.

    Duplicated from :func:`autofix_next.languages.jsts._acquire_shard_lock`
    (duplication accepted — the two adapters have independent shard
    directories).
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


class GoAdapter:
    """``LanguageAdapter`` implementation for Go.

    ``available`` is set at instance construction time from the module
    level probe (AC #15). When False, :meth:`parse_cheap` returns an
    empty-tree ParseResult-shaped object rather than raising.

    Precision for Go is delivered via :meth:`scip_index`, not
    :meth:`parse_precise` — the latter returns ``None`` today.
    """

    language: str = "go"
    extensions: tuple[str, ...] = (".go",)

    def __init__(self) -> None:
        # Re-probe at construction time so the ``available`` attribute
        # reflects the live import state (AC #15). The probe is cheap
        # enough to repeat once per instance.
        try:
            _importlib.import_module("tree_sitter")  # probe only
            _importlib.import_module("tree_sitter_go")  # probe only

            self.available: bool = True
        except ImportError:
            self.available = False

    # ------------------------------------------------------------------
    # Cheap parse path.
    # ------------------------------------------------------------------

    def parse_cheap(self, source: bytes) -> Any:
        """Return a ParseResult-shaped object.

        When the grammar is unavailable, returns an
        :class:`_EmptyParseResult` carrying the original ``source`` bytes
        so downstream callers can introspect without re-reading. One
        ``AdapterRegistered`` envelope row is emitted on first use.
        """

        if not self.available:
            _emit_adapter_registered(
                Path.cwd(),
                language=self.language,
                extensions=self.extensions,
                available=False,
                reason="grammar_missing",
            )
            return _EmptyParseResult(source_bytes=source)

        # Grammar is present — but the cheap tree-sitter-go wiring is not
        # part of task-006's scope. Return an empty-tree ParseResult so
        # the scheduler sees a conforming shape.
        return _EmptyParseResult(source_bytes=source)

    # ------------------------------------------------------------------
    # Precision parse path (returns None per AC #15 derived contract).
    # ------------------------------------------------------------------

    def parse_precise(self, source: bytes) -> Any | None:
        """No tree-sitter precision pass for Go — precision is delivered
        via :meth:`scip_index`. Returns ``None``.
        """

        return None

    # ------------------------------------------------------------------
    # Protocol conformance stubs (symbol_kind / signature).
    # ------------------------------------------------------------------

    def symbol_kind(self, node: Any) -> str:
        """Classify a parse-tree node. Go deferred — returns ``"unknown"``."""

        return "unknown"

    def signature(self, node: Any) -> str:
        """Short signature for a parse-tree node. Go deferred — empty."""

        return ""

    # ------------------------------------------------------------------
    # SCIP index (AC #16, #17, #18, #19, #20, #21).
    # ------------------------------------------------------------------

    def scip_index(
        self,
        workdir: Path,
        changed_files: list[Path] | None = None,
    ) -> Path | None:
        """Invoke ``scip-go`` once per distinct ancestor ``go.mod`` root.

        ``changed_files`` is a list of ``.go`` paths (absolute or relative
        to ``workdir``). Each file is mapped to its nearest-ancestor
        ``go.mod`` directory (AC #16, #18). Files with no ancestor
        ``go.mod`` are silently dropped. Each distinct module root
        computes a cache key

            sha256(module_path + "|" + sha256(go.mod) + "|" + sha256(go.sum or b""))

        and, if the shard already exists on disk, the subprocess is
        skipped and a ``LanguageShardPersisted(cache_mode="reused")``
        row is emitted (AC #17). Otherwise ``scip-go`` is invoked
        exactly once per distinct module root and the result is
        persisted with ``cache_mode="fresh"`` (AC #19, #20).

        :class:`bin_cache.BinaryIntegrityError` is deliberately NOT
        caught — an integrity failure aborts the scan per the
        bin_cache exception contract (AC #29).
        """

        workdir = Path(workdir)
        repo_root = workdir

        # NOTE: ``self.available`` reflects only the tree-sitter-go cheap-path
        # grammar probe (AC #15). SCIP precision is delivered by the
        # ``scip-go`` binary resolved via ``bin_cache`` below — the grammar
        # is NOT required for the SCIP pass, so we do not gate this method
        # on ``self.available``.

        if not changed_files:
            return None

        # 1. Group changed_files by nearest-ancestor go.mod. Files with no
        # ancestor go.mod are silently dropped. AC #21: vendor-dir
        # exclusion is NOT done Python-side — `scip-go` excludes
        # `vendor/` natively.
        module_to_files: dict[Path, list[Path]] = {}
        for rel_or_abs in changed_files:
            f = Path(rel_or_abs)
            if not f.is_absolute():
                f = workdir / f
            mod_root = self._find_module_root(f)
            if mod_root is None:
                _log.debug(
                    "dropping %s: no ancestor go.mod", f
                )
                continue
            module_to_files.setdefault(mod_root, []).append(f)

        if not module_to_files:
            return None

        # 2. Resolve the scip-go binary. A missing / network-failed binary
        # degrades the whole call to cheap-path; integrity errors
        # propagate.
        try:
            bin_path = bin_cache.ensure_binary("scip-go")
        except bin_cache.BinaryUnavailableError:
            _emit_adapter_precision_unavailable(
                repo_root,
                language=self.language,
                reason="binary_download_failed",
            )
            return None

        state_dir = repo_root / ".autofix-next" / "state" / "index" / "scip-go"
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            _emit_adapter_precision_unavailable(
                repo_root,
                language=self.language,
                reason="binary_nonzero_exit",
            )
            return None

        lock_path = state_dir / ".scip-go-lock"
        last_persisted: Path | None = None

        # 3. Iterate distinct module roots; cache-hit short-circuits,
        # cache-miss runs `scip-go` exactly once per module (AC #19).
        try:
            with _acquire_shard_lock(lock_path):
                for mod_root in module_to_files:
                    try:
                        cache_key = self._compute_cache_key(mod_root)
                    except OSError:
                        # Unreadable go.mod / go.sum → treat as
                        # binary_nonzero_exit and skip this module.
                        _emit_adapter_precision_unavailable(
                            repo_root,
                            language=self.language,
                            reason="binary_nonzero_exit",
                        )
                        continue

                    final = state_dir / f"{cache_key}.scip"

                    if final.exists():
                        # AC #17 / #20 — cache hit: skip subprocess.
                        _emit_language_shard_persisted(
                            repo_root,
                            language=self.language,
                            shard_path=str(final),
                            cache_mode="reused",
                            module_root_or_none=str(mod_root),
                        )
                        last_persisted = final
                        continue

                    # Cache miss: invoke scip-go exactly once for this
                    # module root (AC #16, #19).
                    tmp = state_dir / f"{cache_key}.scip.tmp"
                    try:
                        result = subprocess.run(
                            [
                                str(bin_path),
                                "--module",
                                str(mod_root),
                                "--output",
                                str(tmp),
                            ],
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
                        continue

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
                        continue

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
                        continue

                    _emit_language_shard_persisted(
                        repo_root,
                        language=self.language,
                        shard_path=str(final),
                        cache_mode="fresh",
                        module_root_or_none=str(mod_root),
                    )
                    last_persisted = final
        except (BlockingIOError, OSError):
            _emit_adapter_precision_unavailable(
                repo_root,
                language=self.language,
                reason="binary_nonzero_exit",
            )
            return None

        return last_persisted

    # ------------------------------------------------------------------
    # Internal helpers.
    # ------------------------------------------------------------------

    @staticmethod
    def _find_module_root(go_file: Path) -> Path | None:
        """Walk upward from the directory of ``go_file`` and return the
        nearest ancestor that contains a sibling ``go.mod`` file.

        AC #18: nested modules resolve to the INNERMOST ancestor with a
        ``go.mod``. Returns ``None`` when no ancestor has a ``go.mod``.
        """
        try:
            p = go_file.parent.resolve()
        except OSError:
            return None
        while True:
            if (p / "go.mod").is_file():
                return p
            if p == p.parent:
                return None
            p = p.parent

    @staticmethod
    def _compute_cache_key(module_root: Path) -> str:
        """Compute the per-module cache key (AC #17).

        Formula (exact, pinned by the test suite):

            sha256(
                module_path_str
                + "|" + sha256(go.mod bytes).hexdigest()
                + "|" + sha256(go.sum bytes or b"").hexdigest()
            ).hexdigest()
        """
        gomod_hash = hashlib.sha256(
            (module_root / "go.mod").read_bytes()
        ).hexdigest()
        gosum_path = module_root / "go.sum"
        gosum_bytes = b""
        if gosum_path.is_file():
            gosum_bytes = gosum_path.read_bytes()
        gosum_hash = hashlib.sha256(gosum_bytes).hexdigest()
        raw = (
            str(module_root).encode("utf-8")
            + b"|"
            + gomod_hash.encode("utf-8")
            + b"|"
            + gosum_hash.encode("utf-8")
        )
        return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# Self-registration (AC #5).
# ---------------------------------------------------------------------------

register(GoAdapter())


__all__ = ["GoAdapter"]
