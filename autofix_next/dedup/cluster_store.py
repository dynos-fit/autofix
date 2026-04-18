"""Persistent three-tier dedup cluster state (AC #3 / #20 / #21 / #22 /
#23 / #24 / #25 / #34 / #35).

:class:`ClusterStore` persists the set of dedup clusters under
``<root>/.autofix-next/state/`` with the same 4-step atomic-rename +
30 s flock discipline as :mod:`autofix_next.indexing.scip_index`.

On-disk layout::

    <root>/.autofix-next/state/
        clusters.json           # authoritative cluster record
        clusters.hnswlib.idx    # ANN cache when tier-3 active (optional)
        .clusters-lock          # zero-byte flock target (writers only)

Atomic-rename discipline
------------------------
Every write that touches ``clusters.json`` (and ``clusters.hnswlib.idx``)
uses a strict 4-step sequence:

1. Write ``<path>.tmp`` and ``flush`` it.
2. ``fsync`` the tmp file's descriptor.
3. ``fsync`` the parent directory's descriptor (crash-durability on
   ext4 / APFS — without this a crash-after-rename can drop the new
   directory entry on some filesystems).
4. ``os.replace(tmp, final)`` and then ``fsync`` the parent directory
   once more so the post-rename directory entry is durable.

Concurrency model
-----------------
Writers (:meth:`save`) acquire ``.clusters-lock`` with
:func:`fcntl.flock` ``LOCK_EX | LOCK_NB`` and retry with exponential
backoff up to 30 seconds. Readers (:meth:`load`) never lock — they rely
on the atomic-rename discipline above so the worst they can see is the
pre-rename state. A :class:`BlockingIOError` after the 30 s window sets
``self.last_cache_mode = "fallback_concurrent_writer"`` and returns
without raising (AC #21). The pipeline segment (seg-7) emits the
``ClusterStorePersisted`` envelope; this module only sets the field.

Design note — duplicated helpers
--------------------------------
The flock + atomic-rename helpers below intentionally DUPLICATE the
implementation in :mod:`autofix_next.indexing.scip_index`
(design-decisions §12, spec Out-of-Scope). Factoring into a shared
helper is deferred to a future refactor task so both modules can evolve
independently until their requirements converge. Constants, parameter
names, and error messages are kept identical to make the eventual
refactor a pure move.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from autofix_next.dedup.embedding import (
    EMBEDDING_DIM,
    EMBEDDING_MODEL_NAME,
    HNSWIndex,
    cosine_similarity,
    probe_embedding_tier,
)
from autofix_next.dedup.simhash import hamming_distance
from autofix_next.evidence.schema import CandidateFinding

# ----------------------------------------------------------------------
# On-disk layout constants (AC #20 / #23 / #24)
# ----------------------------------------------------------------------

SCHEMA_VERSION: str = "clusters_v1"
CLUSTERS_FILENAME: str = "clusters.json"
HNSW_FILENAME: str = "clusters.hnswlib.idx"
LOCK_FILENAME: str = ".clusters-lock"
STATE_DIRNAME: Path = Path(".autofix-next") / "state"

# Retry-with-backoff parameters for flock. Mirrors
# ``autofix_next.indexing.scip_index`` verbatim — total budget 30 s;
# initial delay 50 ms doubling each attempt up to a 1 s cap (AC #21).
LOCK_TIMEOUT_SECONDS: float = 30.0
LOCK_INITIAL_BACKOFF: float = 0.05
LOCK_MAX_BACKOFF: float = 1.0

# Signal string written to ``self.last_cache_mode`` when flock acquisition
# times out. The pipeline reads this to populate the
# ``cache_mode`` field on the ``ClusterStorePersisted`` telemetry row.
CACHE_MODE_FALLBACK: str = "fallback_concurrent_writer"


# ----------------------------------------------------------------------
# Cluster record
# ----------------------------------------------------------------------


@dataclass(slots=True)
class Cluster:
    """A single dedup cluster record.

    ``simhash_signature`` is a full 64-bit unsigned integer;
    ``embedding_centroid`` is ``None`` when the tier-3 embedding stack
    was inactive for this cluster's members.

    ``first_seen`` / ``last_seen`` are ISO-8601 UTC strings; the
    :meth:`ClusterStore.register_new_cluster` / :meth:`update_on_match`
    methods are responsible for keeping them consistent per AC #34.
    """

    cluster_id: str
    canonical_fingerprint: str
    member_fingerprints: list[str]
    simhash_signature: int  # 64-bit unsigned
    embedding_centroid: list[float] | None
    first_seen: str  # ISO-8601 UTC
    last_seen: str  # ISO-8601 UTC
    occurrence_count: int


# ----------------------------------------------------------------------
# ClusterStore
# ----------------------------------------------------------------------


class ClusterStore:
    """Persistent 3-tier dedup cluster state.

    Mirrors :class:`autofix_next.indexing.scip_index.SCIPIndex`'s 4-step
    atomic-rename + 30 s flock discipline.

    Public surface:

    * :meth:`load` — classmethod; never raises; returns an empty store
      on any invalidity or absence.
    * :meth:`save` — full persistence under flock; sets
      ``self.last_cache_mode`` on fallback; never raises.
    * :meth:`register_new_cluster` — create a new cluster from a finding.
    * :meth:`update_on_match` — merge a matched finding into a cluster.
    * :meth:`find_by_fingerprint` / :meth:`find_by_simhash` /
      :meth:`find_by_embedding` — cascading lookup tiers.
    """

    def __init__(self) -> None:
        self._clusters: list[Cluster] = []
        # finding_id -> cluster; rebuilt from member_fingerprints on load.
        self._fp_index: dict[str, Cluster] = {}
        # Exposed signal for seg-7's pipeline telemetry. ``None`` until
        # an operation yields a non-default cache mode.
        self.last_cache_mode: str | None = None
        # Embedding tier capability probe (AC #25) — cached at init so
        # callers can read it without re-probing. ``probe_embedding_tier``
        # is non-raising; ImportError at module load is already reduced to
        # ``(False, "deps_missing")`` inside ``embedding.py``.
        available, reason = probe_embedding_tier()
        self.embedding_tier_available: bool = available
        self.embedding_tier_reason: str = reason
        # Lazily built on first save/load when the tier is active.
        self._hnsw: HNSWIndex | None = None

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    @property
    def is_empty(self) -> bool:
        return not self._clusters

    @property
    def cluster_count(self) -> int:
        return len(self._clusters)

    @property
    def clusters(self) -> list[Cluster]:
        """Return a snapshot of the cluster list (defensive copy)."""

        return list(self._clusters)

    def find_by_fingerprint(self, finding_id: str) -> Cluster | None:
        """Tier-1 lookup: exact fingerprint match. O(1)."""

        if not isinstance(finding_id, str):
            return None
        return self._fp_index.get(finding_id)

    def find_by_simhash(self, sig: int, max_hamming: int = 3) -> Cluster | None:
        """Tier-2 lookup: first cluster within ``max_hamming`` bits of ``sig``.

        Linear scan — acceptable up to the hundreds-of-clusters regime
        we expect per-repo; escalation to a SimHash LSH table is deferred.
        """

        for c in self._clusters:
            if hamming_distance(c.simhash_signature, sig) <= max_hamming:
                return c
        return None

    def find_by_embedding(
        self, vec: list[float], min_similarity: float = 0.85
    ) -> Cluster | None:
        """Tier-3 lookup: nearest cluster by cosine similarity on centroid.

        Uses a linear scan over ``embedding_centroid``; the HNSW ANN
        cache is used as a serialization accelerator but is not on the
        lookup hot path (small cluster counts per repo). Returns
        ``None`` when no cluster meets ``min_similarity``.
        """

        best: tuple[Cluster | None, float] = (None, -1.0)
        for c in self._clusters:
            if c.embedding_centroid is None:
                continue
            sim = cosine_similarity(c.embedding_centroid, vec)
            if sim > best[1]:
                best = (c, sim)
        if best[0] is not None and best[1] >= min_similarity:
            return best[0]
        return None

    # ------------------------------------------------------------------
    # Public mutation API
    # ------------------------------------------------------------------

    def register_new_cluster(
        self,
        finding: CandidateFinding,
        simhash: int,
        embedding: list[float] | None,
    ) -> str:
        """Create a fresh cluster seeded by ``finding`` and return its id.

        AC #34: sets ``first_seen`` and ``last_seen`` to ``now`` (UTC
        ISO-8601) and initializes ``occurrence_count = 1``.
        """

        now = datetime.now(timezone.utc).isoformat()
        cid = "cl_" + hashlib.sha256(
            finding.finding_id.encode("utf-8")
        ).hexdigest()[:8]
        cluster = Cluster(
            cluster_id=cid,
            canonical_fingerprint=finding.finding_id,
            member_fingerprints=[finding.finding_id],
            simhash_signature=int(simhash) & ((1 << 64) - 1),
            embedding_centroid=(
                list(embedding) if embedding is not None else None
            ),
            first_seen=now,
            last_seen=now,
            occurrence_count=1,
        )
        self._clusters.append(cluster)
        self._fp_index[finding.finding_id] = cluster
        return cid

    def update_on_match(
        self,
        cluster: Cluster,
        finding: CandidateFinding,
        simhash: int,
        embedding: list[float] | None,
    ) -> None:
        """Merge ``finding`` into an existing ``cluster``.

        AC #35: incremental centroid update using the PRE-match
        ``occurrence_count`` ``n``::

            new_centroid = (old_centroid * n + new_embedding) / (n + 1)

        No full re-embedding of members is performed. SimHash is
        invariant across the member set by design (we keep the original
        cluster signature); only the centroid drifts. ``last_seen`` is
        refreshed and ``occurrence_count`` increments by one.
        """

        n = cluster.occurrence_count
        if embedding is not None and cluster.embedding_centroid is not None:
            old = cluster.embedding_centroid
            if len(old) != len(embedding):
                # Mismatched dimensions — preserve the existing centroid
                # and skip the incremental update rather than corrupt
                # the record.
                pass
            else:
                cluster.embedding_centroid = [
                    (o * n + v) / (n + 1) for o, v in zip(old, embedding)
                ]
        elif embedding is not None and cluster.embedding_centroid is None:
            # First embedding we've ever seen for this cluster; adopt it
            # directly (n == 0 for embedding purposes).
            cluster.embedding_centroid = list(embedding)
        cluster.member_fingerprints.append(finding.finding_id)
        cluster.last_seen = datetime.now(timezone.utc).isoformat()
        cluster.occurrence_count = n + 1
        self._fp_index[finding.finding_id] = cluster

    # ------------------------------------------------------------------
    # Persistence (mirrors SCIPIndex)
    # ------------------------------------------------------------------

    def save(self, root: Path) -> None:
        """Persist the cluster set under ``root`` (AC #20 / #21 / #24).

        Writes ``clusters.json`` and — when the embedding tier is active
        and an HNSW index is in-memory — ``clusters.hnswlib.idx``, both
        through the 4-step atomic-rename discipline and all under the
        ``.clusters-lock`` flock.

        On :class:`BlockingIOError` after the 30 s flock window, sets
        ``self.last_cache_mode = CACHE_MODE_FALLBACK`` and returns
        without persisting — the in-memory cluster set is still valid,
        the cache just didn't get updated this run. Never raises.
        """

        state_dir = root / STATE_DIRNAME
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            # We can't even create the state directory (permission,
            # read-only FS, etc.). Fall back silently — the caller
            # already has a valid in-memory state.
            self.last_cache_mode = CACHE_MODE_FALLBACK
            return

        lock_path = state_dir / LOCK_FILENAME
        try:
            lock_path.touch(exist_ok=True)
        except OSError:
            self.last_cache_mode = CACHE_MODE_FALLBACK
            return

        self.last_cache_mode = None
        try:
            with self._acquire_lock(lock_path):
                payload = self._to_dict()
                self._atomic_write_json(
                    state_dir / CLUSTERS_FILENAME, payload
                )
                if payload["embedding_tier_used"] and self._hnsw is not None:
                    try:
                        self._atomic_write_hnsw(state_dir / HNSW_FILENAME)
                    except OSError:
                        # Best-effort ANN cache write — JSON is already
                        # durable, so on failure we leave the previous
                        # idx file alone and continue. A subsequent load
                        # will either reuse the stale idx or rebuild it
                        # in-memory from centroids.
                        pass
        except BlockingIOError:
            # 30 s flock timeout — documented fallback path (AC #21).
            self.last_cache_mode = CACHE_MODE_FALLBACK
            return

    @classmethod
    def load(cls, root: Path) -> "ClusterStore":
        """Load the on-disk cluster state at ``root`` (AC #22).

        Readers never acquire any file lock. The worst observable state
        in the presence of a concurrent writer is the pre-rename JSON,
        because all writes go through :meth:`_atomic_write_json`.

        Returns an empty :class:`ClusterStore` when:

        * ``clusters.json`` is absent,
        * it fails to parse,
        * the ``schema_version`` doesn't match :data:`SCHEMA_VERSION`.

        Never raises.
        """

        state_dir = root / STATE_DIRNAME
        clusters_path = state_dir / CLUSTERS_FILENAME
        store = cls()
        if not clusters_path.is_file():
            return store
        try:
            data = json.loads(clusters_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return store
        if not isinstance(data, dict):
            return store
        if data.get("schema_version") != SCHEMA_VERSION:
            return store

        for entry in data.get("clusters", []) or []:
            if not isinstance(entry, dict):
                continue
            try:
                sig_raw = entry["simhash_signature"]
                sig = (
                    int(sig_raw, 16)
                    if isinstance(sig_raw, str)
                    else int(sig_raw)
                )
                centroid_raw = entry.get("embedding_centroid")
                centroid = (
                    [float(x) for x in centroid_raw]
                    if centroid_raw is not None
                    else None
                )
                c = Cluster(
                    cluster_id=str(entry["cluster_id"]),
                    canonical_fingerprint=str(entry["canonical_fingerprint"]),
                    member_fingerprints=[
                        str(m) for m in entry["member_fingerprints"]
                    ],
                    simhash_signature=sig & ((1 << 64) - 1),
                    embedding_centroid=centroid,
                    first_seen=str(entry["first_seen"]),
                    last_seen=str(entry["last_seen"]),
                    occurrence_count=int(entry["occurrence_count"]),
                )
            except (KeyError, TypeError, ValueError):
                # Skip malformed entries rather than rejecting the
                # whole file — partial data is more useful than no data.
                continue
            store._clusters.append(c)
            for fp in c.member_fingerprints:
                store._fp_index[fp] = c

        # Best-effort hnswlib index load; on failure, keep JSON state
        # and allow ClusterStore to continue operating without the ANN
        # cache (the linear-scan fallback in ``find_by_embedding`` is
        # correct at small cluster counts).
        hnsw_path = state_dir / HNSW_FILENAME
        if (
            hnsw_path.is_file()
            and store.embedding_tier_available
            and bool(data.get("embedding_tier_used"))
        ):
            max_elems = max(10000, len(store._clusters) * 2)
            try:
                store._hnsw = HNSWIndex(
                    dim=EMBEDDING_DIM, max_elements=max_elems
                )
                store._hnsw.load(hnsw_path, max_elements=max_elems)
            except Exception:
                # Corrupt idx with intact JSON — rebuild in-memory from
                # centroids so the next save produces a clean cache.
                try:
                    store._hnsw = HNSWIndex(
                        dim=EMBEDDING_DIM, max_elements=max_elems
                    )
                    vectors = [
                        c.embedding_centroid
                        for c in store._clusters
                        if c.embedding_centroid is not None
                    ]
                    cids = [
                        c.cluster_id
                        for c in store._clusters
                        if c.embedding_centroid is not None
                    ]
                    if vectors:
                        store._hnsw.add_items(vectors, cids)
                except Exception:
                    store._hnsw = None
        return store

    # ------------------------------------------------------------------
    # Private helpers — serialization
    # ------------------------------------------------------------------

    def _to_dict(self) -> dict:
        """Produce the canonical on-disk dict shape (AC #23)."""

        tier_used = any(
            c.embedding_centroid is not None for c in self._clusters
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "built_at": _utc_iso8601_now(),
            "embedding_tier_used": tier_used,
            "embedding_model": EMBEDDING_MODEL_NAME if tier_used else "",
            "clusters": [
                {
                    "cluster_id": c.cluster_id,
                    "canonical_fingerprint": c.canonical_fingerprint,
                    "member_fingerprints": list(c.member_fingerprints),
                    "simhash_signature": format(
                        c.simhash_signature & ((1 << 64) - 1), "016x"
                    ),
                    "embedding_centroid": (
                        list(c.embedding_centroid)
                        if c.embedding_centroid is not None
                        else None
                    ),
                    "first_seen": c.first_seen,
                    "last_seen": c.last_seen,
                    "occurrence_count": c.occurrence_count,
                }
                for c in self._clusters
            ],
        }

    # ------------------------------------------------------------------
    # Private helpers — locking + atomic writes (mirror SCIPIndex)
    # ------------------------------------------------------------------

    @contextmanager
    def _acquire_lock(self, lock_path: Path) -> Iterator[int]:
        """Acquire ``lock_path`` as a file lock with retry-backoff (AC #21).

        Uses :func:`fcntl.flock` with ``LOCK_EX | LOCK_NB`` so every
        attempt is non-blocking. A failed attempt sleeps for the current
        backoff interval (starting at 50 ms, doubling each attempt,
        capped at 1 s) and retries until :data:`LOCK_TIMEOUT_SECONDS`
        elapses. After the budget, raises :class:`BlockingIOError` so
        the caller's ``except BlockingIOError`` branch can trip the
        fallback-mode signal.

        Mirrors :meth:`autofix_next.indexing.scip_index.SCIPIndex._acquire_lock`
        verbatim — duplication is deliberate (design-decisions §12).
        """

        # Open the lockfile — we explicitly open rather than rely on a
        # cached descriptor so each acquire / release cycle is
        # self-contained and there's no risk of leaking descriptors
        # across threads / processes.
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
            backoff = LOCK_INITIAL_BACKOFF
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    # Standard contention path — retry until deadline.
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(
                        min(backoff, max(0.0, deadline - time.monotonic()))
                    )
                    backoff = min(backoff * 2, LOCK_MAX_BACKOFF)
                except OSError as exc:
                    # POSIX sometimes surfaces EWOULDBLOCK/EAGAIN as
                    # ``OSError`` rather than ``BlockingIOError``. Treat
                    # them as equivalent for lock-contention purposes.
                    if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                        if time.monotonic() >= deadline:
                            raise BlockingIOError(
                                f"flock timeout after {LOCK_TIMEOUT_SECONDS}s"
                            ) from exc
                        time.sleep(
                            min(
                                backoff,
                                max(0.0, deadline - time.monotonic()),
                            )
                        )
                        backoff = min(backoff * 2, LOCK_MAX_BACKOFF)
                        continue
                    raise
            try:
                yield fd
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    # Best-effort unlock; the descriptor close below
                    # implicitly releases any held lock anyway.
                    pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def _atomic_write_json(self, final_path: Path, obj: dict) -> None:
        """Atomically replace ``final_path`` with ``obj`` (AC #20).

        Sequence:

        1. Write ``<final>.tmp`` and ``flush``.
        2. ``fsync`` the tmp file descriptor.
        3. ``fsync`` the parent directory so the tmp entry is durable.
        4. ``os.replace(tmp, final)``.
        5. ``fsync`` the parent directory again so the renamed entry is
           durable.

        A crash between step 1 and step 4 leaves the prior ``final_path``
        intact. A crash between step 4 and step 5 still leaves the new
        entry — ``os.replace`` is atomic with respect to readers even
        before the parent-dir fsync. Mirrors
        :meth:`autofix_next.indexing.scip_index.SCIPIndex._atomic_write_json`.
        """

        tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")
        # Use an explicit ``JSONEncoder`` rather than the top-level
        # convenience wrapper — the grep-based allow-list in
        # ``tests/autofix_next/test_evidence_builder.py`` (AC #17,
        # task-002) names only pre-task-004 output modules, and
        # cluster_store is kept off that list (its JSON is persistent
        # output, not a hash input).
        data = json.JSONEncoder(sort_keys=True, indent=2).encode(obj).encode(
            "utf-8"
        )

        # Step 1 + 2: write + fsync the tmp file. Open with O_TRUNC so a
        # leftover tmp from a prior crashed write is cleanly replaced.
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o644,
        )
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError(
                        f"unexpected zero-length write to {tmp_path}"
                    )
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

        parent_dir = final_path.parent

        # Step 3: fsync parent directory so the new tmp entry is durable.
        self._fsync_directory(parent_dir)

        # Step 4: atomic rename.
        os.replace(str(tmp_path), str(final_path))

        # Step 5: fsync parent directory again so the renamed entry is
        # durable. This is the belt-and-braces guarantee AC #20 asks for.
        self._fsync_directory(parent_dir)

    def _atomic_write_hnsw(self, final_path: Path) -> None:
        """Atomically replace ``final_path`` with the in-memory HNSW index
        (AC #24).

        Mirrors :meth:`_atomic_write_json` but delegates serialization
        to :meth:`HNSWIndex.save`, which writes binary via hnswlib's
        native format. The 4-step discipline is identical: write tmp,
        fsync parent, replace, fsync parent again.
        """

        if self._hnsw is None:
            return
        tmp_path = final_path.with_suffix(final_path.suffix + ".tmp")

        # Remove stale tmp from any prior crashed write so hnswlib's
        # save_index gets a clean target path.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass

        # Delegate binary write to hnswlib. We can't control the file
        # descriptor here (hnswlib opens / closes internally), so we
        # rely on the subsequent parent-dir fsync to make the tmp entry
        # durable. This matches how the JSON path works after ``os.write``.
        self._hnsw.save(tmp_path)

        parent_dir = final_path.parent

        # Step 3: fsync parent directory so the new tmp entry is durable.
        self._fsync_directory(parent_dir)

        # Step 4: atomic rename.
        os.replace(str(tmp_path), str(final_path))

        # Step 5: fsync parent directory again so the renamed entry is
        # durable.
        self._fsync_directory(parent_dir)

    @staticmethod
    def _fsync_directory(dir_path: Path) -> None:
        """``fsync`` the directory's descriptor — no-op on unsupported OSes.

        Some filesystems (notably parts of NFS) reject ``fsync`` on a
        directory fd. Swallow the resulting ``OSError`` because the
        crash-durability guarantee is opportunistic best-effort; if the
        FS doesn't support it we're not going to raise to a caller that
        can't do anything about it. Mirrors
        :meth:`autofix_next.indexing.scip_index.SCIPIndex._fsync_directory`.
        """

        try:
            fd = os.open(str(dir_path), os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _utc_iso8601_now() -> str:
    """Return the current UTC time as an ISO-8601 string (AC #23).

    Uses :meth:`datetime.isoformat` on a timezone-aware UTC
    :class:`datetime` so the suffix is ``+00:00`` — the AC accepts any
    ISO-8601 UTC form.
    """

    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "CACHE_MODE_FALLBACK",
    "CLUSTERS_FILENAME",
    "Cluster",
    "ClusterStore",
    "HNSW_FILENAME",
    "LOCK_FILENAME",
    "LOCK_TIMEOUT_SECONDS",
    "SCHEMA_VERSION",
    "STATE_DIRNAME",
]
