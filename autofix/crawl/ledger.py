"""Append-only JSONL crawl ledger (ARCH-016).

The ledger records one row per ``(bundle_fingerprint, analyzer)``
scan. Same on-disk discipline as the workflow state machine:
JSONL with ``O_APPEND`` for byte-level atomicity. Multiple
processes can record concurrently; each line is one row, no
half-merges.

Public surface:

* :class:`LedgerRow` — frozen dataclass mirroring the JSONL row.
* :class:`Ledger` — in-memory index + persistence. ``record(row)``
  appends to disk; ``replay_from_disk()`` rebuilds the index from
  the file.

Half-written lines (e.g. from a process killed mid-write) are
skipped with a stderr warning during replay; the surrounding rows
are still consumed.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from autofix.crawl.crawl_constants import LEDGER_FILENAME


def _parse_iso_z(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class LedgerRow:
    """One bundle-scan record. Immutable on construction.

    The first nine fields are the original (v1) schema. The remaining
    four fields were appended in v2 — every one defaults to ``None``
    so old-format JSONL rows parse without error and so writers that
    don't yet populate them produce on-disk lines that are
    byte-identical to the v1 format (``to_jsonl_line`` filters None
    keys out).
    """

    ts: str
    bundle_fingerprint: str
    seed_path: str
    file_paths: tuple[str, ...]
    analyzer: str
    last_commit_sha: str
    last_finding_count: int
    cache_hit: bool
    event_id: str
    # --- v2 optional fields (appended; never reordered) -----------------
    scan_count_for_seed: int | None = None
    imported_by_count_at_scan: int | None = None
    bundle_size_bytes: int | None = None
    budget_hit_reason: str | None = None

    def to_jsonl_line(self) -> str:
        """Render this row as one JSONL line (newline-terminated).

        Keys whose value is ``None`` are filtered out so v2 writers
        producing rows with no v2 data emit lines indistinguishable
        from v1 — protects backward-compat for any external reader
        that wasn't updated for v2.
        """
        d = asdict(self)
        # tuple → list for JSON.
        d["file_paths"] = list(self.file_paths)
        d = {k: v for k, v in d.items() if v is not None}
        return json.dumps(d, separators=(",", ":")) + "\n"

    @classmethod
    def from_dict(cls, d: dict) -> "LedgerRow":
        scan_count = d.get("scan_count_for_seed")
        imported_by = d.get("imported_by_count_at_scan")
        bundle_size = d.get("bundle_size_bytes")
        return cls(
            ts=d["ts"],
            bundle_fingerprint=d["bundle_fingerprint"],
            seed_path=d["seed_path"],
            file_paths=tuple(d["file_paths"]),
            analyzer=d["analyzer"],
            last_commit_sha=d["last_commit_sha"],
            last_finding_count=int(d["last_finding_count"]),
            cache_hit=bool(d["cache_hit"]),
            event_id=d["event_id"],
            scan_count_for_seed=(
                int(scan_count) if scan_count is not None else None
            ),
            imported_by_count_at_scan=(
                int(imported_by) if imported_by is not None else None
            ),
            bundle_size_bytes=(
                int(bundle_size) if bundle_size is not None else None
            ),
            budget_hit_reason=d.get("budget_hit_reason"),
        )


class Ledger:
    """In-memory + on-disk crawl ledger."""

    def __init__(self, *, root: Path) -> None:
        self._root = Path(root)
        self._rows: list[LedgerRow] = []

    # --- Persistence ------------------------------------------------------

    @property
    def log_path(self) -> Path:
        return self._root / ".autofix" / LEDGER_FILENAME

    def record(self, row: LedgerRow) -> None:
        """Append ``row`` to the on-disk ledger AND in-memory index.

        Uses ``O_APPEND`` for byte-level atomicity — concurrent
        writers cannot interleave bytes within a line.
        """
        log = self.log_path
        log.parent.mkdir(parents=True, exist_ok=True)
        line = row.to_jsonl_line()
        fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        self._rows.append(row)

    def replay_from_disk(self) -> None:
        """Rebuild the in-memory index from the JSONL file.

        Half-written / malformed lines are skipped with a stderr
        warning; the surrounding rows are still consumed.
        """
        self._rows = []
        log = self.log_path
        if not log.exists():
            return
        with log.open("r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    self._rows.append(LedgerRow.from_dict(d))
                except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                    print(
                        f"autofix: ledger {log} line {line_no} unreadable; skipping",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue

    # --- Query ------------------------------------------------------------

    def __iter__(self):
        return iter(self._rows)

    def latest_for(self, key, analyzer):
        """Return the most-recent LedgerRow for the ``(key, analyzer)`` pair.

        ``key`` may be either a ``bundle_fingerprint`` (the
        canonical case) or a ``Path`` (test mocks), and may carry a
        ``None`` analyzer when scoring per-file freshness without a
        bound analyzer. The lookup is best-effort.
        """
        key_str = str(key) if hasattr(key, "__fspath__") else key
        latest: LedgerRow | None = None
        for r in self._rows:
            if analyzer is not None and r.analyzer != analyzer:
                continue
            if isinstance(key_str, str) and len(key_str) == 64 and all(
                c in "0123456789abcdef" for c in key_str
            ):
                # Looks like a fingerprint — match against bundle_fingerprint.
                if r.bundle_fingerprint != key_str:
                    continue
            else:
                # Match against any file in the row's file_paths.
                if key_str not in r.file_paths:
                    continue
            if latest is None or r.ts > latest.ts:
                latest = r
        return latest

    def bundle_appearance_count_in_window(
        self, path, window_start: str, now: str
    ) -> int:
        """Count distinct bundles ``path`` appeared in within
        ``[window_start, now]``.

        Two ledger rows with the same ``bundle_fingerprint`` count as
        ONE bundle (different analyzers on the same bundle are still
        one bundle for saturation purposes).

        Path matching tolerates absolute / relative / basename
        variants between writer and reader.
        """
        candidates = self._path_match_set(path)
        start = _parse_iso_z(window_start)
        end = _parse_iso_z(now)
        seen_fingerprints: set[str] = set()
        for r in self._rows:
            if not any(fp in candidates for fp in r.file_paths):
                continue
            ts = _parse_iso_z(r.ts)
            if ts < start or ts > end:
                continue
            seen_fingerprints.add(r.bundle_fingerprint)
        return len(seen_fingerprints)

    def _path_match_set(self, path) -> set[str]:
        """Return the set of canonical path strings to match against
        ledger rows — accommodates absolute / relative / basename
        variants.
        """
        p = Path(str(path))
        out = {str(p), p.name}
        if p.is_absolute():
            try:
                out.add(str(p.relative_to(self._root)))
            except ValueError:
                pass
        return out


__all__ = ["Ledger", "LedgerRow"]
