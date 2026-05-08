"""LLM-judgment analyzer base class.

Provides a templated pattern for subclasses to invoke free-form LLM judgment
via cached prompts. Subclasses define the prompt template and category,
while the base class handles caching, telemetry, and error recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from autofix.evidence.schema import CandidateFinding
from autofix.indexing.symbols import SymbolTable
from autofix.llm.scheduler import AnalyzerSeamUnavailableError, Scheduler
from autofix.parsing.tree_sitter import ParseResult
from autofix.telemetry import events_log
from autofix.telemetry.correlation import current_commit_sha, current_scan_id

# Per-scan memoization: maps scan_id -> set of event types already logged.
# This ensures we log "AnalyzerUnavailable" and "AnalyzerError" at most once per scan.
_PER_SCAN_EVENTS: dict[str, set[str]] = {}


def _resolve_repo_root(parse_result: "ParseResult") -> Path:
    """Derive the repository root from a ParseResult.

    ``ParseResult`` exposes ``path`` (absolute) and ``relpath``
    (repo-relative). The class does NOT expose a ``repo_root``
    attribute — earlier code in this module wrongly assumed it
    did, which crashed at runtime the moment any LLM-judgment
    analyzer fired against a real ``parse_file`` output.

    The relationship is ``path == repo_root / relpath``, so
    walking ``path``'s parents by ``len(Path(relpath).parts) - 1``
    recovers the repo root regardless of how deeply nested the
    file is.
    """
    rel_parts = Path(parse_result.relpath).parts
    if not rel_parts:
        return parse_result.path.parent
    # ``path.parents[0]`` is the directory containing ``path``;
    # ``path.parents[k]`` walks up ``k+1`` components. We want to
    # discard exactly ``len(rel_parts)`` components from the right.
    walk = len(rel_parts) - 1
    return parse_result.path.parents[walk]


def _should_log_event(scan_id: str, event_type: str) -> bool:
    """Return True if we should log this event for this scan (first time only).

    Updates the internal memoization set.
    """
    if scan_id not in _PER_SCAN_EVENTS:
        _PER_SCAN_EVENTS[scan_id] = set()
    if event_type in _PER_SCAN_EVENTS[scan_id]:
        return False
    _PER_SCAN_EVENTS[scan_id].add(event_type)
    return True


class LLMJudgmentAnalyzer(ABC):
    """Base class for LLM-judgment analyzers.

    Subclasses must define:
    - RULE_ID_PREFIX (class attribute)
    - prompt_template (classmethod)

    The base class handles caching, file reading, JSON parsing, and telemetry.
    """

    RULE_ID_PREFIX: str  # Subclass-pinned identifier prefix
    RULE_VERSION: str = "v1"
    MODEL: str = "opus"

    @classmethod
    @abstractmethod
    def prompt_template(cls, diff_context: str) -> str:
        """Generate the LLM prompt for this analyzer.

        Parameters
        ----------
        diff_context : str
            Contextual information about the code diff or region under analysis.

        Returns
        -------
        str
            The prompt string to send to the LLM.

        Raises
        ------
        NotImplementedError
            If the subclass does not override this method.
        """
        raise NotImplementedError

    @classmethod
    def analyze(
        cls, parse_result: ParseResult, symbol_table: SymbolTable
    ) -> Iterable[CandidateFinding]:
        """Analyze a file via LLM judgment with caching.

        AC-3 flow:
        1. Read file at parse_result.path as UTF-8 (after deriving repo_root from path + relpath).
           On OSError or UnicodeDecodeError: return empty iter, NO event.
        2. Generate prompt via prompt_template (propagates NotImplementedError).
        3. Resolve commit_sha (from parse_result, current_commit_sha, or sentinel).
        4. Compute cache key (sha256).
        5. Check cache (version 1 match returns cached findings).
        6. Cache miss: invoke_judgment via Scheduler.
           On AnalyzerSeamUnavailableError: log once per scan, return empty iter.
        7. Parse JSON: validate list-of-dicts with required keys.
           On failure: log AnalyzerError once per scan, return empty iter.
        8. Build CandidateFinding per item.
        9. Persist to cache atomically (tempfile + replace).
        10. Return findings.

        Parameters
        ----------
        parse_result : ParseResult
            Output of parse_file (carries ``path``, ``relpath``, the
            tree-sitter ``tree``, ``source_bytes``, and ``lines``).
            Repo root is derived inside this method via
            :func:`_resolve_repo_root`.
        symbol_table : SymbolTable
            Unused, required by analyzer interface.

        Yields
        ------
        CandidateFinding
            Zero or more findings from LLM judgment.
        """
        # Step 0: Resolve repo root from parse_result.path + relpath.
        repo_root = _resolve_repo_root(parse_result)

        # Step 1: Read file (OSError/UnicodeDecodeError -> return empty, no event)
        file_path = parse_result.path
        try:
            source_code = file_path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            return iter([])

        # Resolve scan_id for telemetry
        scan_id = current_scan_id() or "_no_scan"

        # Step 2: Generate prompt (NotImplementedError propagates)
        prompt = cls.prompt_template(source_code)

        # Step 3: Resolve commit_sha
        commit_sha = getattr(parse_result, "commit_sha", None)
        if not commit_sha:
            commit_sha = current_commit_sha() or "_no_commit"

        # Step 4: Compute cache key
        cache_input = (prompt + commit_sha + cls.MODEL).encode("utf-8")
        cache_key = hashlib.sha256(cache_input).hexdigest()

        # Defense-in-depth: validate cache_key format before joining
        # paths. ``assert`` would be stripped under ``python -O``, so we
        # raise explicitly to keep the path-safety invariant intact.
        if not re.fullmatch(r"[0-9a-f]{64}", cache_key):
            raise RuntimeError(f"Invalid cache_key: {cache_key}")

        # Step 5: Check cache
        cache_dir = repo_root / ".autofix" / "cache" / "llm_judgment"
        cache_path = cache_dir / f"{cache_key}.json"

        cached_findings = cls._try_read_cache(
            cache_path,
            expected_key=cache_key,
            expected_model=cls.MODEL,
            expected_commit_sha=commit_sha,
        )
        if cached_findings is not None:
            yield from cached_findings
            return

        # Step 6: Cache miss — invoke LLM
        try:
            scheduler = Scheduler(root=repo_root)
            raw_response = scheduler.invoke_judgment(prompt, model=cls.MODEL)
        except AnalyzerSeamUnavailableError:
            if _should_log_event(scan_id, "AnalyzerUnavailable"):
                try:
                    events_log.append_event(
                        repo_root,
                        "AnalyzerUnavailable",
                        {"analyzer": cls.RULE_ID_PREFIX, "scan_id": scan_id},
                    )
                except OSError:
                    pass
            return iter([])

        # Step 7: Parse and validate JSON
        try:
            parsed = json.loads(raw_response)
        except (json.JSONDecodeError, ValueError):
            if _should_log_event(scan_id, "AnalyzerError"):
                try:
                    events_log.append_event(
                        repo_root,
                        "AnalyzerError",
                        {
                            "analyzer": cls.RULE_ID_PREFIX,
                            "scan_id": scan_id,
                            "file": str(parse_result.relpath),
                            "reason": "Failed to parse JSON response",
                            "raw": raw_response[:1024],
                        },
                    )
                except OSError:
                    pass
            return iter([])

        # Validate shape: must be list of dicts with required keys
        if not isinstance(parsed, list):
            if _should_log_event(scan_id, "AnalyzerError"):
                try:
                    events_log.append_event(
                        repo_root,
                        "AnalyzerError",
                        {
                            "analyzer": cls.RULE_ID_PREFIX,
                            "scan_id": scan_id,
                            "file": str(parse_result.relpath),
                            "reason": f"JSON root is {type(parsed).__name__}, expected list",
                            "raw": raw_response[:1024],
                        },
                    )
                except OSError:
                    pass
            return iter([])

        findings: list[CandidateFinding] = []
        for item in parsed:
            if not isinstance(item, dict):
                # Skip stray non-dict entries
                continue

            # Validate required keys
            required_keys = {"category", "severity", "description", "start_line", "end_line", "evidence"}
            if not all(k in item for k in required_keys):
                if _should_log_event(scan_id, "AnalyzerError"):
                    try:
                        events_log.append_event(
                            repo_root,
                            "AnalyzerError",
                            {
                                "analyzer": cls.RULE_ID_PREFIX,
                                "scan_id": scan_id,
                                "file": str(parse_result.relpath),
                                "reason": f"Item missing required keys. Has: {set(item.keys())}",
                                "raw": raw_response[:1024],
                            },
                        )
                    except OSError:
                        pass
                return iter([])

            try:
                # Step 8: Build CandidateFinding
                rule_id = f"{cls.RULE_ID_PREFIX}:{item['category']}"
                provenance = f"{cls.RULE_ID_PREFIX}:{cls.MODEL}:{cache_key[:16]}"
                path = parse_result.relpath
                symbol_name = item.get("symbol_name", item["category"])
                start_line = int(item["start_line"])
                end_line = int(item["end_line"])
                changed_slice = str(item["description"])
                normalized_import = ""

                finding = CandidateFinding(
                    rule_id=rule_id,
                    path=path,
                    symbol_name=symbol_name,
                    normalized_import=normalized_import,
                    start_line=start_line,
                    end_line=end_line,
                    changed_slice=changed_slice,
                    finding_id="",
                    provenance=provenance,
                )
                findings.append(finding)
            except (KeyError, ValueError, TypeError):
                # Skip malformed items
                continue

        # Step 9: Persist findings to cache atomically
        cls._write_cache(
            cache_path,
            cache_key,
            findings,
            commit_sha,
            repo_root,
            scan_id,
        )

        # Step 10: Return findings
        yield from findings

    @classmethod
    def _try_read_cache(
        cls,
        cache_path: Path,
        expected_key: str,
        expected_model: str,
        expected_commit_sha: str,
    ) -> list[CandidateFinding] | None:
        """Try to read cached findings.

        Audit sec-001 fix: validate that the on-disk envelope's identity
        fields match the freshly-derived inputs. A cache file whose
        ``key`` / ``model`` / ``commit_sha`` does not match the expected
        values is treated as a miss — defense against cache-poisoning
        where an attacker (or filesystem race) places a crafted
        envelope under a colliding path. The cache_key derivation is
        SHA-256-strong, but trusting only the path-derived key without
        re-validating the stored envelope leaves a TOCTOU window.

        Returns None if the cache file doesn't exist, is invalid, has
        mismatched version, or fails identity validation. Returns
        empty list if cache exists, validates, and contains no findings.
        """
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                envelope = json.load(f)
        except (FileNotFoundError, IsADirectoryError, OSError):
            return None
        except (json.JSONDecodeError, ValueError):
            return None

        # Validate envelope version
        if not isinstance(envelope, dict) or envelope.get("version") != 1:
            return None

        # Audit sec-001: validate envelope identity matches expected inputs.
        # A non-matching key, model, or commit_sha indicates the cache file
        # was placed by a different invocation (or maliciously) and must
        # NOT be trusted for the current inputs.
        if envelope.get("key") != expected_key:
            return None
        if envelope.get("model") != expected_model:
            return None
        if envelope.get("commit_sha") != expected_commit_sha:
            return None

        # Extract findings from cache
        cached_items = envelope.get("findings", [])
        findings: list[CandidateFinding] = []

        for item in cached_items:
            if not isinstance(item, dict):
                continue
            try:
                finding = CandidateFinding(
                    rule_id=item["rule_id"],
                    path=item["path"],
                    symbol_name=item["symbol_name"],
                    normalized_import=item.get("normalized_import", ""),
                    start_line=int(item["start_line"]),
                    end_line=int(item["end_line"]),
                    changed_slice=item.get("changed_slice", ""),
                    finding_id=item.get("finding_id", ""),
                    provenance=item.get("provenance", ""),
                )
                findings.append(finding)
            except (KeyError, ValueError, TypeError):
                continue

        return findings

    @classmethod
    def _write_cache(
        cls,
        cache_path: Path,
        cache_key: str,
        findings: list[CandidateFinding],
        commit_sha: str,
        repo_root: Path,
        scan_id: str,
    ) -> None:
        """Persist findings to cache atomically.

        Uses tempfile + Path.replace pattern with 0o600 permissions.
        Logs warning on OSError during write but still returns (doesn't raise).
        """
        # Create cache directory if needed
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # Log warning and continue
            try:
                events_log.append_event(
                    repo_root,
                    "AnalyzerWarning",
                    {
                        "analyzer": cls.RULE_ID_PREFIX,
                        "scan_id": scan_id,
                        "reason": f"Failed to create cache directory: {e}",
                    },
                )
            except OSError:
                pass
            return

        # Build envelope
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        findings_dicts = [
            {
                "rule_id": f.rule_id,
                "path": f.path,
                "symbol_name": f.symbol_name,
                "normalized_import": f.normalized_import,
                "start_line": f.start_line,
                "end_line": f.end_line,
                "changed_slice": f.changed_slice,
                "finding_id": f.finding_id,
                "provenance": f.provenance,
            }
            for f in findings
        ]
        envelope = {
            "version": 1,
            "key": cache_key,
            "model": cls.MODEL,
            "commit_sha": commit_sha,
            "created_at": now_utc,
            "findings": findings_dicts,
        }
        envelope_json = json.dumps(envelope, separators=(",", ":"))

        # Write atomically via tempfile + replace
        tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
        try:
            fd = os.open(
                str(tmp_path),
                # O_EXCL+O_CREAT means the open fails if the file
                # already exists, so O_TRUNC is unreachable; omit it.
                os.O_CREAT | os.O_WRONLY | os.O_EXCL,
                0o600,
            )
            try:
                os.write(fd, envelope_json.encode("utf-8"))
            finally:
                os.close(fd)
            tmp_path.replace(cache_path)
        except OSError as e:
            # Best-effort cleanup of tempfile
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            # Log warning but don't raise
            try:
                events_log.append_event(
                    repo_root,
                    "AnalyzerWarning",
                    {
                        "analyzer": cls.RULE_ID_PREFIX,
                        "scan_id": scan_id,
                        "reason": f"Failed to write cache: {e}",
                    },
                )
            except OSError:
                pass

    @classmethod
    def _reset_per_scan_state(cls) -> None:
        """Clear the per-scan memoization state.

        Called by tests and scan runners to reset state between scans.
        Not part of the public API.
        """
        _PER_SCAN_EVENTS.clear()


__all__ = ["LLMJudgmentAnalyzer"]
