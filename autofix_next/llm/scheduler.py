# AD-9: benchmark_trace_llm thread-safety verified against
# /Users/hassam/Documents/agent-bench/agent_bench/decorators.py and
# /Users/hassam/Documents/agent-bench/agent_bench/session.py.
# The decorator reads `current_session()` which is a `threading.local()`
# stack: worker threads spawned by `ThreadPoolExecutor` have their own
# empty stack and therefore take the `session is None` pass-through
# branch, leaving `run_prompt` unwrapped and thread-safe. Even if a
# session were active on a worker thread, recording writes land in
# thread-local-scoped list(s) and are not shared across workers, so no
# cross-thread mutation without locking occurs.
"""LLM scheduler — tiered, cached, bounded-concurrency gate pipeline.

This module is the SOLE boundary between ``autofix_next`` and the locked
LLM seam exposed by :mod:`autofix.llm_backend`. Every other module under
``autofix_next/`` reaches the LLM through this scheduler; a grep test in
``tests/autofix_next/test_llm_scheduler.py::test_run_prompt_is_only_llm_seam``
enforces the boundary.

Per-packet 7-step gate pipeline (``_process_one``)
--------------------------------------------------

1. **Suppression glob**. If ``packet.primary_symbol``'s path part matches
   a pattern in ``autofix-policy.json::suppressions``, emit
   ``LLMCallGated{decision="skipped_suppressed"}`` and return.
2. **Generated-code gate**. Skipped wholesale when
   ``llm_tiered.generated_disabled`` is ``true``. Otherwise: path-glob
   test against the default+policy list first, then a first-2KB marker
   scan (case-insensitive substring) with a NUL-byte binary heuristic.
   Read errors fail open.
3. **Priority threshold**. ``priority is None`` → small tier (and emit
   ``promoted_default_tier`` at step 7). ``priority < small_threshold``
   → ``skipped_below_threshold`` and return. ``priority >= large_threshold``
   → large tier. Otherwise small tier.
4. **Dedup**. One critical section under ``_dedup_lock``: check membership
   and insert the ``prompt_prefix_hash`` atomically. Membership →
   ``skipped_duplicate_hash``.
5. **Persistent cache lookup**. A fresh, schema-matched,
   non-expired entry under ``<cache_root>/<hh>/<hash>.json`` yields
   ``promoted_cache_hit`` without consuming budget or calling run_prompt.
6. **Budget**. One critical section under ``_budget_lock``: check >0 and
   decrement. Exhaustion → ``skipped_budget_exceeded`` with a reason that
   includes ``call_budget=<N>``.
7. **Promote**. Resolve the (rule_id, tier) template via
   :func:`autofix_next.llm.triage.resolve_template`; read once under the
   template-bytes cache; assemble ``template + "\\n" + canonical_json``;
   call ``_llm_backend.run_prompt``; persist to cache; emit
   ``promoted`` / ``promoted_default_tier`` / ``promoted_failed`` /
   ``cache_store_failed`` as appropriate.

Concurrency
-----------

``schedule_many`` submits each ``(packet, priority)`` to a stdlib
``ThreadPoolExecutor(max_workers=self._max_inflight)`` and collects
results in INPUT ORDER via ``future.result()``. No semaphore is added —
the pool IS the concurrency cap. ``schedule`` is a thin wrapper over
``schedule_many([(packet, priority)])[0]``.
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import fnmatch
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Literal

# NOTE: this is the single authorized import from ``autofix.*`` anywhere
# under ``autofix_next/``. Enforced by
# ``tests/autofix_next/test_llm_scheduler.py::test_run_prompt_is_only_llm_seam``.
import autofix.llm_backend as _llm_backend
from autofix.llm_backend import LLMBackendConfig, LLMResult

from autofix_next.evidence.fingerprints import canonical_json_bytes
from autofix_next.evidence.schema import EvidencePacket
from autofix_next.llm import triage
from autofix_next.telemetry import events_log
from autofix_next.telemetry.atomic import atomic_write_json
from autofix_next.telemetry.correlation import current_commit_sha, current_scan_id
from autofix_next.telemetry.replay import _REPLAY_ACTIVE
from autofix_next.telemetry.tracer import span

# Decision literal shared by the scheduler's public surface and by the
# ``LLMCallGated`` event payload. The exact ten-member set is pinned by
# AC 5 and by ``tests/autofix_next/test_llm_scheduler_decisions.py
# ::test_schedule_decision_name_has_exactly_ten_members``.
ScheduleDecisionName = Literal[
    "promoted",
    "skipped_suppressed",
    "skipped_duplicate_hash",
    "promoted_failed",
    "skipped_generated",
    "skipped_below_threshold",
    "skipped_budget_exceeded",
    "promoted_cache_hit",
    "promoted_default_tier",
    "cache_store_failed",
]


# ---------------------------------------------------------------------------
# Hard-coded defaults (AC 8, AC 9).
# ---------------------------------------------------------------------------

_DEFAULT_GENERATED_GLOBS: tuple[str, ...] = (
    "**/vendor/**",
    "**/node_modules/**",
    "**/dist/**",
    "**/build/**",
    "**/.generated/**",
    "*.min.js",
    "*.min.css",
    "*.pb.go",
    "*_pb2.py",
    "*_pb2.pyi",
    "**/__pycache__/**",
)

_DEFAULT_GENERATED_MARKERS: tuple[str, ...] = (
    "DO NOT EDIT",
    "Code generated by",
    "@generated",
    "AUTOGENERATED",
)

_CACHE_SCHEMA_VERSION: str = "llm_cache_v1"
_DEFAULT_CACHE_TTL_SECONDS: int = 1209600  # 14 days
_DEFAULT_SMALL_MODEL: str = "haiku"
_DEFAULT_LARGE_MODEL: str = "sonnet"
_DEFAULT_SMALL_THRESHOLD: float = 0.30
_DEFAULT_LARGE_THRESHOLD: float = 0.70
_DEFAULT_MAX_INFLIGHT: int = 4
_DEFAULT_CALL_BUDGET: int = 50
_MARKER_SCAN_LIMIT: int = 2048
_ISO_FORMAT: str = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(slots=True)
class ScheduleDecision:
    """The outcome of a single :meth:`Scheduler.schedule` call.

    Fields are ordered ``decision``, ``result``, ``reason`` (AC 4) with
    unchanged types (``ScheduleDecisionName``, ``LLMResult | None``,
    ``str | None``).
    """

    decision: ScheduleDecisionName
    result: LLMResult | None
    reason: str | None = None


@dataclass(slots=True, frozen=True)
class _TieredPolicy:
    """Parsed ``llm_tiered.*`` subdict with defaults applied."""

    small_threshold: float
    large_threshold: float
    small_model: str
    large_model: str
    max_inflight: int
    call_budget: int
    cache_dir: str | None
    cache_ttl_seconds: int
    generated_globs: tuple[str, ...]
    generated_markers: tuple[str, ...]
    generated_disabled: bool


class Scheduler:
    """Gate LLM calls behind the 7-step pipeline and emit telemetry.

    One instance per scan. In-memory state (dedup set, budget counter,
    template-bytes cache) is per-instance and resets when a new Scheduler
    is constructed (see AC 34, AC 44, AC 49).
    """

    def __init__(
        self,
        root: Path,
        policy_path: Path | None = None,
        llm_config: LLMBackendConfig | None = None,
        timeout: int = 60,
        prompt_template_path: Path | None = None,
    ) -> None:
        # Keep this signature byte-identical to the pre-task-008 form (AC 1).
        self._root: Path = Path(root)
        self._timeout: int = int(timeout)
        self._config: LLMBackendConfig = llm_config or LLMBackendConfig()

        # Legacy single-template override: only honored for the
        # ``(unused-import.intra-file, small)`` pair by ``triage.resolve_template``.
        self._prompt_template_path_override: Path | None = (
            Path(prompt_template_path) if prompt_template_path is not None else None
        )
        self._prompts_dir: Path = Path(__file__).parent / "prompts"

        resolved_policy = (
            Path(policy_path)
            if policy_path is not None
            else self._root / ".autofix" / "autofix-policy.json"
        )
        self._suppressions: list[str] = self._load_suppressions(resolved_policy)
        tiered = self._load_tiered_policy(resolved_policy)

        # Fail-loud threshold check (AD-6, AC 15).
        if tiered.large_threshold < tiered.small_threshold:
            raise ValueError(
                "llm_tiered.large_model_threshold="
                f"{tiered.large_threshold} must be >= "
                f"small_model_threshold={tiered.small_threshold}"
            )
        if tiered.max_inflight <= 0:
            raise ValueError(
                f"llm_tiered.max_inflight={tiered.max_inflight} must be > 0"
            )
        if tiered.call_budget <= 0:
            raise ValueError(
                f"llm_tiered.call_budget={tiered.call_budget} must be > 0"
            )

        self._small_threshold: float = tiered.small_threshold
        self._large_threshold: float = tiered.large_threshold
        self._small_model: str = tiered.small_model
        self._large_model: str = tiered.large_model
        self._max_inflight: int = tiered.max_inflight
        # Immutable reference copy of the configured budget, retained so the
        # ``skipped_budget_exceeded`` reason string can still cite the
        # original value after ``_budget_remaining`` has been decremented.
        self._call_budget: int = tiered.call_budget
        self._cache_ttl_seconds: int = tiered.cache_ttl_seconds
        self._generated_disabled: bool = tiered.generated_disabled
        self._generated_globs: tuple[str, ...] = (
            tuple(_DEFAULT_GENERATED_GLOBS) + tiered.generated_globs
        )
        self._generated_markers: tuple[str, ...] = (
            tuple(_DEFAULT_GENERATED_MARKERS) + tiered.generated_markers
        )

        # Per-instance locks + state (AC 34, AC 44, AC 49).
        self._dedup_lock: threading.Lock = threading.Lock()
        self._budget_lock: threading.Lock = threading.Lock()
        self._template_lock: threading.Lock = threading.Lock()
        self._called_hashes: set[str] = set()
        self._budget_remaining: int = tiered.call_budget
        self._template_bytes_cache: dict[tuple[str, str], str] = {}

        # Cache-root resolution: env → policy → per-scan-root default (AC 36).
        self._cache_root_path: Path = self._resolve_cache_root(
            tiered.cache_dir, self._root
        )

        # Concurrency pool (AD-2, AC 46).
        self._executor: concurrent.futures.ThreadPoolExecutor = (
            concurrent.futures.ThreadPoolExecutor(max_workers=self._max_inflight)
        )

    # ------------------------------------------------------------------
    # Policy loading
    # ------------------------------------------------------------------

    @staticmethod
    def _load_suppressions(policy_path: Path) -> list[str]:
        """Return the top-level ``suppressions`` list from policy, or []."""
        data = Scheduler._read_policy(policy_path)
        if not isinstance(data, dict):
            return []
        suppressions = data.get("suppressions")
        if not isinstance(suppressions, list):
            return []
        return [p for p in suppressions if isinstance(p, str)]

    @staticmethod
    def _load_tiered_policy(policy_path: Path) -> _TieredPolicy:
        """Parse the ``llm_tiered`` subdict into a ``_TieredPolicy`` with defaults."""
        data = Scheduler._read_policy(policy_path)
        if not isinstance(data, dict):
            return _TieredPolicy(
                small_threshold=_DEFAULT_SMALL_THRESHOLD,
                large_threshold=_DEFAULT_LARGE_THRESHOLD,
                small_model=_DEFAULT_SMALL_MODEL,
                large_model=_DEFAULT_LARGE_MODEL,
                max_inflight=_DEFAULT_MAX_INFLIGHT,
                call_budget=_DEFAULT_CALL_BUDGET,
                cache_dir=None,
                cache_ttl_seconds=_DEFAULT_CACHE_TTL_SECONDS,
                generated_globs=(),
                generated_markers=(),
                generated_disabled=False,
            )
        raw = data.get("llm_tiered")
        if not isinstance(raw, dict):
            raw = {}

        def _num(value: Any, default: float) -> float:
            if isinstance(value, bool):
                return default
            if isinstance(value, (int, float)):
                return float(value)
            return default

        def _str(value: Any, default: str) -> str:
            if isinstance(value, str) and value != "":
                return value
            return default

        def _int_or_default(value: Any, default: int) -> int:
            if isinstance(value, bool):
                return default
            if isinstance(value, int):
                return value
            return default

        def _str_list(value: Any) -> tuple[str, ...]:
            if not isinstance(value, list):
                return ()
            return tuple(item for item in value if isinstance(item, str))

        small_t = _num(raw.get("small_model_threshold"), _DEFAULT_SMALL_THRESHOLD)
        large_t = _num(raw.get("large_model_threshold"), _DEFAULT_LARGE_THRESHOLD)
        small_m = _str(raw.get("small_model"), _DEFAULT_SMALL_MODEL)
        large_m = _str(raw.get("large_model"), _DEFAULT_LARGE_MODEL)
        max_in = _int_or_default(raw.get("max_inflight"), _DEFAULT_MAX_INFLIGHT)
        budget = _int_or_default(raw.get("call_budget"), _DEFAULT_CALL_BUDGET)
        ttl = _int_or_default(
            raw.get("cache_ttl_seconds"), _DEFAULT_CACHE_TTL_SECONDS
        )
        cache_dir_raw = raw.get("cache_dir")
        cache_dir = cache_dir_raw if isinstance(cache_dir_raw, str) and cache_dir_raw else None
        generated_disabled = bool(raw.get("generated_disabled", False)) if isinstance(
            raw.get("generated_disabled", False), bool
        ) else False

        return _TieredPolicy(
            small_threshold=small_t,
            large_threshold=large_t,
            small_model=small_m,
            large_model=large_m,
            max_inflight=max_in,
            call_budget=budget,
            cache_dir=cache_dir,
            cache_ttl_seconds=ttl,
            generated_globs=_str_list(raw.get("generated_globs")),
            generated_markers=_str_list(raw.get("generated_markers")),
            generated_disabled=generated_disabled,
        )

    @staticmethod
    def _read_policy(policy_path: Path) -> Any:
        """Return parsed policy JSON, or ``None`` when absent.

        Fail-closed on read error / malformed JSON by raising
        ``ValueError`` (AD-7).
        """
        if not policy_path.is_file():
            return None
        try:
            raw = policy_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(
                f"cannot read autofix-policy.json at {policy_path}: {exc}"
            ) from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"autofix-policy.json at {policy_path} is not valid JSON: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Cache-root resolution (AC 36)
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_cache_root(policy_cache_dir: str | None, scan_root: Path) -> Path:
        """Resolve the cache root: env var → policy → per-scan-root default.

        The default lives under the scheduler's ``root`` (i.e. the repo being
        scanned) rather than a per-user ``~/.cache/`` path. This prevents
        cross-repo cache leakage and keeps tests whose ``root`` is a
        ``tmp_path`` from polluting the user cache. AC 36 default amended
        to ``<scan_root>/.cache/autofix-next/llm_prompt_prefix``.
        """
        env_value = os.environ.get("AUTOFIX_NEXT_LLM_CACHE_DIR")
        if env_value:
            return Path(env_value).expanduser()
        if policy_cache_dir:
            return Path(policy_cache_dir).expanduser()
        return scan_root / ".cache" / "autofix-next" / "llm_prompt_prefix"

    # ------------------------------------------------------------------
    # Suppression check
    # ------------------------------------------------------------------

    def _matches_suppression(self, primary_symbol: str) -> bool:
        """Return ``True`` if ``primary_symbol``'s path portion matches a glob."""
        path_part = self._path_part(primary_symbol)
        for pattern in self._suppressions:
            if fnmatch.fnmatch(path_part, pattern):
                return True
        return False

    @staticmethod
    def _path_part(primary_symbol: str) -> str:
        """Strip the ``::<symbol>`` suffix from a ``primary_symbol``."""
        if "::" in primary_symbol:
            return primary_symbol.split("::", 1)[0]
        return primary_symbol

    # ------------------------------------------------------------------
    # Generated-code gate (AC 7–13)
    # ------------------------------------------------------------------

    def _match_generated_glob(self, path_part: str) -> bool:
        """Return True when ``path_part`` matches any configured generated-glob.

        Uses ``PurePosixPath.full_match`` for ``**``-bearing patterns and
        falls back to ``fnmatch.fnmatch`` against the basename for bare
        file-extension globs such as ``*.min.js``.
        """
        if not path_part:
            return False
        posix = PurePosixPath(path_part)
        basename = posix.name
        for pattern in self._generated_globs:
            try:
                if posix.full_match(pattern):
                    return True
            except (ValueError, TypeError):
                # full_match raises on empty / malformed patterns; fall
                # through to fnmatch so the scan still has a chance.
                pass
            if fnmatch.fnmatch(basename, pattern):
                return True
        return False

    def _is_generated(self, primary_symbol: str) -> bool:
        """Return ``True`` when the primary-symbol file is generated code.

        Glob match is attempted first against ``primary_symbol``'s path
        portion (POSIX-style repo-relative path). On glob miss the first
        2 KB of the file is read and scanned for a NUL byte (binary) or a
        case-insensitive marker substring. Absolute paths or paths whose
        resolved location escapes ``self._root`` fail open (AC 11).
        """
        path_part = self._path_part(primary_symbol)

        # Glob pre-check (AC 7, AC 8). Two-engine matcher:
        #   1. ``PurePosixPath.full_match`` handles ``**``-bearing recursive
        #      patterns (e.g. ``**/vendor/**``).
        #   2. ``fnmatch.fnmatch`` on the basename handles bare-file globs
        #      (e.g. ``*.min.js`` against ``static/main.min.js``).
        if self._match_generated_glob(path_part):
            return True

        # Reject any absolute path or path that would escape the repo root
        # BEFORE touching the filesystem (sec prevention rule; AC 11 fail-open).
        if not path_part or path_part.startswith("/"):
            return False
        try:
            candidate = (self._root / path_part).resolve()
            root_resolved = self._root.resolve()
        except OSError:
            return False
        try:
            candidate.relative_to(root_resolved)
        except ValueError:
            return False

        # Marker scan (AC 9, AC 10, AC 11).
        try:
            with open(candidate, "rb") as fh:
                head = fh.read(_MARKER_SCAN_LIMIT)
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return False
        except OSError:
            return False

        if b"\x00" in head:
            return True

        try:
            text = head.decode("utf-8", errors="replace").lower()
        except Exception:
            return False
        for marker in self._generated_markers:
            if marker.lower() in text:
                return True
        return False

    # ------------------------------------------------------------------
    # Template resolution (AC 27, AC 32, AC 33, AC 49)
    # ------------------------------------------------------------------

    def _resolve_template_bytes(self, rule_id: str, tier: Literal["small", "large"]) -> str:
        """Return template text for ``(rule_id, tier)``, reading at most once.

        Fast-path: single-key dict lookup (GIL-atomic per AC 49). Slow
        path: acquire ``_template_lock`` and populate, re-checking to
        avoid duplicate reads on the narrow race.
        """
        key = (rule_id, tier)
        cached = self._template_bytes_cache.get(key)
        if cached is not None:
            return cached
        with self._template_lock:
            cached = self._template_bytes_cache.get(key)
            if cached is not None:
                return cached
            legacy_override = self._prompt_template_path_override
            resolved = triage.resolve_template(
                self._prompts_dir, rule_id, tier, legacy_override
            )
            if not resolved.is_file():
                raise FileNotFoundError(
                    f"LLM prompt template missing for rule_id={rule_id!r} "
                    f"tier={tier!r}: attempted path={resolved}"
                )
            # Use ``Path.read_text`` (not ``open().read()``) so the
            # read-once behavior is observable by tests that patch
            # ``pathlib.Path.read_text`` (see
            # ``test_template_bytes_read_once_across_same_rule_tier``).
            text = resolved.read_text(encoding="utf-8")
            self._template_bytes_cache[key] = text
            return text

    # ------------------------------------------------------------------
    # Persistent prompt-prefix cache (AC 36–43, AD-4, AD-5, AD-10)
    # ------------------------------------------------------------------

    def _cache_entry_path(self, prompt_prefix_hash: str) -> Path:
        """Return the shard path ``<root>/<hh>/<hash>.json``."""
        shard = prompt_prefix_hash[:2] if len(prompt_prefix_hash) >= 2 else prompt_prefix_hash
        return self._cache_root_path / shard / f"{prompt_prefix_hash}.json"

    def _cache_get(
        self, prompt_prefix_hash: str, tier: Literal["small", "large"]
    ) -> LLMResult | None:
        """Return a reconstructed ``LLMResult`` on a fresh hit, else ``None``.

        Treats as miss on:
         * file missing
         * decode error
         * ``schema_version != 'llm_cache_v1'`` (AD-10)
         * un-parseable ``created_at_iso``
         * TTL expiration — also best-effort ``unlink`` (AC 40)
         * stored ``tier`` does NOT match the currently-routed tier
        """
        path = self._cache_entry_path(prompt_prefix_hash)
        try:
            raw = path.read_text(encoding="utf-8")
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return None
        except OSError:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        if data.get("schema_version") != _CACHE_SCHEMA_VERSION:
            return None
        stored_tier = data.get("tier")
        if stored_tier != tier:
            # Tier mismatch — treat as miss so the requested tier can run
            # through ``run_prompt`` and overwrite the stored entry.
            return None
        created_at = data.get("created_at_iso")
        ttl = data.get("ttl_seconds")
        if not isinstance(created_at, str) or not isinstance(ttl, int):
            return None
        try:
            created_dt = datetime.strptime(created_at, _ISO_FORMAT).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None
        now = datetime.now(timezone.utc)
        if now > created_dt + timedelta(seconds=ttl):
            # Best-effort eviction (AC 40).
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            return None
        returncode = data.get("returncode")
        stdout = data.get("stdout")
        stderr = data.get("stderr")
        if not isinstance(returncode, int) or not isinstance(stdout, str) or not isinstance(stderr, str):
            return None
        return LLMResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def _cache_put(
        self,
        *,
        prompt_prefix_hash: str,
        tier: str,
        model: str,
        result: LLMResult,
    ) -> bool:
        """Atomically persist ``result`` under the sharded cache layout.

        Returns ``True`` on success, ``False`` on any ``OSError`` during
        directory creation, tmp write, fsync, or replace (AC 39, AC 43).

        Delegates the 4-step ``tmp -> fsync -> os.replace -> dir-fsync``
        install to :func:`autofix_next.telemetry.atomic.atomic_write_json`
        (task-20260417-010 seg-4). That helper serializes with
        ``sort_keys=True`` + ``separators=(",", ":")`` — byte-identical to
        the prior :func:`canonical_json_bytes` route the cache previously
        used, so the on-disk format that ``_cache_get`` reads back is
        preserved.
        """
        final = self._cache_entry_path(prompt_prefix_hash)
        payload: dict[str, Any] = {
            "schema_version": _CACHE_SCHEMA_VERSION,
            "tier": tier,
            "model": model,
            "returncode": int(result.returncode),
            "stdout": str(result.stdout),
            "stderr": str(result.stderr),
            "created_at_iso": datetime.now(timezone.utc).strftime(_ISO_FORMAT),
            "ttl_seconds": int(self._cache_ttl_seconds),
        }
        # ``atomic_write_json`` owns: parent mkdir, tmp write, fd fsync,
        # best-effort parent-dir fsync (before and after), ``os.replace``,
        # and best-effort tmp unlink on any OSError. It returns ``True``
        # on success / ``False`` on any OSError in the install path,
        # matching the pre-existing ``_cache_put`` contract that
        # ``_process_one`` relies on to emit ``cache_store_failed``.
        return atomic_write_json(final, payload)

    # ------------------------------------------------------------------
    # Telemetry helpers
    # ------------------------------------------------------------------

    def _emit_gated_event(
        self,
        *,
        decision: ScheduleDecisionName,
        packet: EvidencePacket,
        reason: str | None = None,
    ) -> None:
        """Append exactly one ``LLMCallGated`` row describing ``decision``.

        Telemetry-write errors are swallowed — the scan must still produce
        a meaningful ``ScheduleDecision`` for the caller even when the
        events.jsonl path is unwritable.
        """
        payload: dict[str, Any] = {
            "event_type": "LLMCallGated",
            "repo_id": self._root.name,
            "decision": decision,
            "prompt_prefix_hash": packet.prompt_prefix_hash,
            "primary_symbol": packet.primary_symbol,
            "rule_id": packet.rule_id,
        }
        if reason is not None:
            payload["reason"] = reason
        try:
            events_log.append_event(self._root, "LLMCallGated", payload)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    def schedule(
        self, packet: EvidencePacket, *, priority: float | None = None
    ) -> ScheduleDecision:
        """Apply the 7-step pipeline to a single packet.

        Delegates to :meth:`schedule_many` so ``run_prompt`` still flows
        through the concurrency pool even for one-shot callers (AC 46).
        """
        return self.schedule_many([(packet, priority)])[0]

    def schedule_many(
        self,
        batch: Iterable[tuple[EvidencePacket, float | None]],
    ) -> list[ScheduleDecision]:
        """Process ``batch`` concurrently, returning decisions in INPUT ORDER."""
        items = list(batch)
        if not items:
            return []
        # SPEC-001 repair: ThreadPoolExecutor.submit does NOT automatically
        # propagate contextvars (AC-9 assumption). Wrap each submission in
        # copy_context().run(...) so correlation (scan_id, commit_sha,
        # event_id), replay mode (_REPLAY_ACTIVE, _REPLAY_EVENTS_SINK), and
        # the OTel span context all flow into worker threads.
        futures: list[concurrent.futures.Future[ScheduleDecision]] = [
            self._executor.submit(
                contextvars.copy_context().run,
                self._process_one,
                packet,
                priority,
            )
            for (packet, priority) in items
        ]
        # AD-3: collect in input order (``future.result()`` blocks until done).
        return [fut.result() for fut in futures]

    # ------------------------------------------------------------------
    # Per-packet worker
    # ------------------------------------------------------------------

    def _process_one(
        self, packet: EvidencePacket, priority: float | None
    ) -> ScheduleDecision:
        """Run the 7-step pipeline for a single ``(packet, priority)`` pair."""
        with span(
            "autofix_next.schedule",
            scan_id=current_scan_id(),
            commit_sha=current_commit_sha(),
            rule_id=packet.rule_id,
            prompt_prefix_hash=packet.prompt_prefix_hash,
        ) as sched_span:

            def _stamp(
                tier_val: str | None, decision_obj: ScheduleDecision
            ) -> ScheduleDecision:
                sched_span.set_attribute("tier", tier_val or "none")
                sched_span.set_attribute("decision", decision_obj.decision)
                return decision_obj

            # Step 1: suppression glob (AC 6).
            if self._matches_suppression(packet.primary_symbol):
                reason = "path matches a configured suppression glob"
                self._emit_gated_event(
                    decision="skipped_suppressed", packet=packet, reason=reason
                )
                return _stamp(
                    None,
                    ScheduleDecision(
                        decision="skipped_suppressed", result=None, reason=reason
                    ),
                )

            # Step 2: generated-code gate (AC 7–13).
            if not self._generated_disabled and self._is_generated(
                packet.primary_symbol
            ):
                reason = "primary_symbol file classified as generated code"
                self._emit_gated_event(
                    decision="skipped_generated", packet=packet, reason=reason
                )
                return _stamp(
                    None,
                    ScheduleDecision(
                        decision="skipped_generated", result=None, reason=reason
                    ),
                )

            # Step 3: priority threshold (AC 18–21, AC 48).
            is_default_tier = priority is None
            if priority is not None and priority < self._small_threshold:
                reason = (
                    f"priority={priority} below small_model_threshold="
                    f"{self._small_threshold}"
                )
                self._emit_gated_event(
                    decision="skipped_below_threshold",
                    packet=packet,
                    reason=reason,
                )
                return _stamp(
                    None,
                    ScheduleDecision(
                        decision="skipped_below_threshold",
                        result=None,
                        reason=reason,
                    ),
                )
            if priority is not None and priority >= self._large_threshold:
                tier: Literal["small", "large"] = "large"
                resolved_model = self._large_model
            else:
                tier = "small"
                resolved_model = self._small_model

            # Step 4: dedup check-and-insert (AC 34, AC 35). Key includes the
            # routed tier so a same-hash packet that re-enters at a different
            # tier (e.g. priority=0.95 after an earlier priority=0.50) is still
            # routable to the other model — matching the behavior asserted by
            # ``tests/autofix_next/test_llm_scheduler_tiered.py
            # ::test_model_default_fallbacks``.
            prompt_prefix_hash = packet.prompt_prefix_hash
            dedup_key = f"{prompt_prefix_hash}:{tier}"
            with self._dedup_lock:
                if dedup_key in self._called_hashes:
                    duplicate = True
                else:
                    self._called_hashes.add(dedup_key)
                    duplicate = False
            if duplicate:
                reason = "prompt_prefix_hash already seen this scan"
                self._emit_gated_event(
                    decision="skipped_duplicate_hash",
                    packet=packet,
                    reason=reason,
                )
                return _stamp(
                    tier,
                    ScheduleDecision(
                        decision="skipped_duplicate_hash",
                        result=None,
                        reason=reason,
                    ),
                )

            # Step 5: persistent cache lookup (AC 42, AC 44). A cache hit is
            # accepted only when the stored entry's tier matches the
            # currently-routed tier; a tier-swap re-promotes the packet.
            cached = self._cache_get(prompt_prefix_hash, tier)
            if cached is not None:
                self._emit_gated_event(
                    decision="promoted_cache_hit", packet=packet
                )
                return _stamp(
                    tier,
                    ScheduleDecision(
                        decision="promoted_cache_hit", result=cached, reason=None
                    ),
                )

            # Step 6: budget check-and-decrement (AC 44, AC 45).
            with self._budget_lock:
                if self._budget_remaining <= 0:
                    over_budget = True
                else:
                    self._budget_remaining -= 1
                    over_budget = False
            if over_budget:
                reason = (
                    f"call_budget={self._call_budget} exhausted; "
                    "no remaining LLM invocations this scan"
                )
                self._emit_gated_event(
                    decision="skipped_budget_exceeded",
                    packet=packet,
                    reason=reason,
                )
                return _stamp(
                    tier,
                    ScheduleDecision(
                        decision="skipped_budget_exceeded",
                        result=None,
                        reason=reason,
                    ),
                )

            # Step 7: template resolution + run_prompt + cache write.
            # Replay-mode gate: short-circuit before any LLM call (AC 42).
            if _REPLAY_ACTIVE.get():
                self._emit_gated_event(
                    decision="promoted",
                    packet=packet,
                    reason="replay-mode",
                )
                return _stamp(
                    tier,
                    ScheduleDecision(
                        decision="promoted", result=None, reason="replay-mode"
                    ),
                )

            template_text = self._resolve_template_bytes(packet.rule_id, tier)
            packet_json_text = canonical_json_bytes(packet.to_dict()).decode("utf-8")
            prompt = template_text + "\n" + packet_json_text

            with span(
                "autofix_next.llm_call",
                scan_id=current_scan_id(),
                commit_sha=current_commit_sha(),
                rule_id=packet.rule_id,
                tier=tier,
                model=resolved_model,
            ) as llm_span:
                try:
                    result = _llm_backend.run_prompt(
                        prompt,
                        model=resolved_model,
                        config=self._config,
                        timeout=self._timeout,
                        cwd=self._root,
                    )
                except Exception as exc:  # noqa: BLE001 - telemetry reports the class.
                    llm_span.set_attribute("returncode", -1)
                    reason = f"{type(exc).__name__}: {exc}"
                    self._emit_gated_event(
                        decision="promoted_failed", packet=packet, reason=reason
                    )
                    return _stamp(
                        tier,
                        ScheduleDecision(
                            decision="promoted_failed",
                            result=None,
                            reason=reason,
                        ),
                    )
                llm_span.set_attribute(
                    "returncode",
                    result.returncode
                    if hasattr(result, "returncode")
                    else -1,
                )

            if not isinstance(result, LLMResult) or result.returncode != 0:
                rc = getattr(result, "returncode", "unknown")
                reason = f"returncode={rc}"
                self._emit_gated_event(
                    decision="promoted_failed", packet=packet, reason=reason
                )
                return _stamp(
                    tier,
                    ScheduleDecision(
                        decision="promoted_failed", result=None, reason=reason
                    ),
                )

            stored = self._cache_put(
                prompt_prefix_hash=prompt_prefix_hash,
                tier=tier,
                model=resolved_model,
                result=result,
            )
            if not stored:
                reason = "cache write failed (OSError)"
                self._emit_gated_event(
                    decision="cache_store_failed", packet=packet, reason=reason
                )
                # Spec AC 43: still surface the live LLMResult as ``promoted``.
                return _stamp(
                    tier,
                    ScheduleDecision(
                        decision="promoted", result=result, reason=None
                    ),
                )

            success_decision: ScheduleDecisionName = (
                "promoted_default_tier" if is_default_tier else "promoted"
            )
            if is_default_tier:
                self._emit_gated_event(
                    decision="promoted_default_tier",
                    packet=packet,
                    reason="promoted on default small-tier path (priority=None)",
                )
            else:
                self._emit_gated_event(decision="promoted", packet=packet)
            return _stamp(
                tier,
                ScheduleDecision(
                    decision=success_decision, result=result, reason=None
                ),
            )


__all__ = [
    "Scheduler",
    "ScheduleDecision",
    "ScheduleDecisionName",
]
