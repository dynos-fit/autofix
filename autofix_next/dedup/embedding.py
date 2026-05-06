"""Optional embedding tier (tier 3) — sentence-transformers + hnswlib.

Imports are at module TOP so a missing dep surfaces immediately via
``ImportError``; :func:`probe_embedding_tier` catches that plus model-load
failures and returns a structured ``(bool, reason)`` so
:class:`~autofix_next.dedup.cluster_store.ClusterStore` can fall back to
SimHash-only when the tier is unavailable.

Public surface (AC #25):

* :data:`EMBEDDING_MODEL_NAME` — frozen model id (``all-MiniLM-L6-v2``).
* :data:`EMBEDDING_DIM` — vector dimension (``384``).
* :class:`EmbeddingTier` — per-scan-root owner of the model-cache
  directory (task-010 AC 10/11). Holds ``self._cache_dir`` resolved
  from env > policy > ``root / ".cache/autofix-next/embedding"``.
* :func:`probe_embedding_tier` — non-raising capability probe.
* :func:`embed_text` — compute a sentence-transformer embedding.
* :func:`cosine_similarity` — pure-math similarity (no numpy required).
* :class:`HNSWIndex` — thin wrapper around :mod:`hnswlib` for ANN search.

Cache-dir resolution (task-010 META-003 / AC 10-11)
---------------------------------------------------
The previous module-level ``EMBEDDING_CACHE_DIR = Path("~/.cache/autofix-next/models/")``
constant leaked per-operator/cross-repo state. :class:`EmbeddingTier` now
resolves the cache directory at instance construction time via
:func:`_resolve_embedding_cache_dir`, which honours (in order):

1. Environment variable ``AUTOFIX_NEXT_EMBEDDING_CACHE_DIR``.
2. ``policy["embedding_tier"]["cache_dir"]``.
3. Default ``scan_root / ".cache/autofix-next/embedding"``.

Operators who previously relied on a shared ``~/.cache`` model cache
will see a cold-cache cost on the first scan per repo; this matches
the AC-36 per-scan-root policy established in task-008.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

EMBEDDING_MODEL_NAME: str = "all-MiniLM-L6-v2"
EMBEDDING_DIM: int = 384

# Module-top imports per AC #25. Wrap in a single try so test collection
# never fails on a machine without the extras — the optional [dedup]
# extras group in pyproject.toml carries the real dependency on
# sentence-transformers + hnswlib.
try:
    import sentence_transformers  # type: ignore[import-not-found]
    import hnswlib  # type: ignore[import-not-found]

    _IMPORT_OK = True
    _IMPORT_ERROR: BaseException | None = None
except ImportError as exc:  # pragma: no cover - exercised on machines w/o extras
    sentence_transformers = None  # type: ignore[assignment]
    hnswlib = None  # type: ignore[assignment]
    _IMPORT_OK = False
    _IMPORT_ERROR = exc


# ----------------------------------------------------------------------
# Cache-directory resolution (task-010 AC 10)
# ----------------------------------------------------------------------


def _resolve_embedding_cache_dir(
    scan_root: Path, policy: dict | None
) -> Path:
    """Resolve the embedding model cache directory for a scan.

    Resolution order (first wins):

    1. ``AUTOFIX_NEXT_EMBEDDING_CACHE_DIR`` environment variable.
    2. ``policy["embedding_tier"]["cache_dir"]`` (when ``policy`` is a
       dict with the nested ``embedding_tier.cache_dir`` key).
    3. ``scan_root / ".cache/autofix-next/embedding"``.

    The returned path is not materialised on disk — creation is deferred
    to the model-load step that actually writes into the cache. Every
    branch returns a :class:`pathlib.Path` so callers can uniformly
    call ``.mkdir(parents=True, exist_ok=True)``.
    """

    env_override = os.environ.get("AUTOFIX_NEXT_EMBEDDING_CACHE_DIR")
    if env_override:
        return Path(env_override).expanduser()

    if isinstance(policy, dict):
        tier_cfg = policy.get("embedding_tier")
        if isinstance(tier_cfg, dict):
            policy_override = tier_cfg.get("cache_dir")
            if isinstance(policy_override, (str, Path)) and policy_override:
                return Path(policy_override).expanduser()

    # Default — per-scan-root isolation (META-003 fix).
    return Path(scan_root) / ".cache" / "autofix-next" / "embedding"


# ----------------------------------------------------------------------
# EmbeddingTier — owning class for the model-cache dir (AC 11)
# ----------------------------------------------------------------------


class EmbeddingTier:
    """Per-scan-root owner of the sentence-transformer model cache.

    Construction resolves ``self._cache_dir`` via
    :func:`_resolve_embedding_cache_dir` and memoises a single loaded
    model instance on first :meth:`embed_text` call. The tier is safe
    to construct even when the optional extras are absent —
    :meth:`probe` returns ``(False, "deps_missing")`` and
    :meth:`embed_text` raises :class:`RuntimeError` instead of silently
    producing bogus vectors.

    Parameters
    ----------
    root:
        The scan root — used as the final fallback for cache-dir
        resolution. Every scan supplies its own root so two concurrent
        scans against different repos never share a cache directory.
    policy:
        Optional policy dict; ``policy["embedding_tier"]["cache_dir"]``
        (when present) overrides the default cache location but is
        itself overridden by ``AUTOFIX_NEXT_EMBEDDING_CACHE_DIR``.
    """

    def __init__(
        self, root: Path, policy: dict | None = None
    ) -> None:
        self._root: Path = Path(root)
        self._policy: dict | None = policy
        self._cache_dir: Path = _resolve_embedding_cache_dir(
            self._root, policy
        )
        self._model: Any = None

    @property
    def cache_dir(self) -> Path:
        """The resolved cache directory for this tier instance."""

        return self._cache_dir

    def probe(self) -> tuple[bool, str]:
        """Return ``(available, reason)`` for this instance's cache dir.

        Same semantics as the module-level :func:`probe_embedding_tier`
        but checks ``self._cache_dir`` first instead of a process-wide
        constant.
        """

        if not _IMPORT_OK:
            return (False, "deps_missing")
        offline = (
            os.environ.get("HF_HUB_OFFLINE") == "1"
            or os.environ.get("TRANSFORMERS_OFFLINE") == "1"
        )
        cached = (self._cache_dir / EMBEDDING_MODEL_NAME).exists() or (
            Path(
                "~/.cache/torch/sentence_transformers/sentence-transformers_"
                + EMBEDDING_MODEL_NAME
            )
            .expanduser()
            .exists()
        )
        if offline and not cached:
            return (False, "model_cache_missing_offline")
        return (True, "available")

    def _load_model(self) -> Any:
        """Lazily load and memoise the sentence-transformer model.

        Callers must ensure ``_IMPORT_OK`` first — :meth:`embed_text` does.
        """

        if self._model is None:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            self._model = sentence_transformers.SentenceTransformer(
                EMBEDDING_MODEL_NAME, cache_folder=str(self._cache_dir)
            )
        return self._model

    def embed_text(self, text: str) -> list[float]:
        """Return an ``EMBEDDING_DIM``-length float list for ``text``."""

        if not _IMPORT_OK:
            raise RuntimeError(
                "embedding tier unavailable: deps_missing"
            ) from _IMPORT_ERROR
        model = self._load_model()
        vec = model.encode(
            text, convert_to_numpy=True, show_progress_bar=False
        )
        return [float(x) for x in vec.tolist()]


# ----------------------------------------------------------------------
# Backward-compatible module-level shims (preserve existing callers)
# ----------------------------------------------------------------------

# A process-wide default tier rooted at the current working directory is
# kept solely to preserve the pre-task-010 call-site signatures in
# ``cluster_store.py`` (``probe_embedding_tier()``) and ``cascade.py``
# (``embed_text(text)``). Long-lived callers SHOULD construct their own
# :class:`EmbeddingTier` with an explicit ``root`` — the default tier
# does NOT participate in per-scan-root cache isolation.
_default_tier: "EmbeddingTier | None" = None


def _get_default_tier() -> "EmbeddingTier":
    global _default_tier
    if _default_tier is None:
        _default_tier = EmbeddingTier(Path.cwd())
    return _default_tier


def probe_embedding_tier() -> tuple[bool, str]:
    """Return ``(available, reason)`` for the embedding tier.

    ``reason`` is one of:

    * ``"available"`` — deps importable and (if offline) the model cache
      is on disk.
    * ``"deps_missing"`` — ``sentence_transformers`` or ``hnswlib``
      failed to import at module load.
    * ``"model_cache_missing_offline"`` — deps are fine but the process
      is offline (``HF_HUB_OFFLINE=1`` or ``TRANSFORMERS_OFFLINE=1``)
      and the model cache is absent; downloading would fail so we
      refuse to enable the tier.

    This probe does NOT trigger a model download. Uses a process-wide
    default :class:`EmbeddingTier` rooted at ``Path.cwd()``; new code
    should construct an :class:`EmbeddingTier` explicitly.
    """

    return _get_default_tier().probe()


def embed_text(text: str) -> list[float]:
    """Return an ``EMBEDDING_DIM``-length float list for ``text``.

    Raises :class:`RuntimeError` if the embedding tier is not available
    — callers should gate via :func:`probe_embedding_tier` first.

    Uses a process-wide default :class:`EmbeddingTier` rooted at
    ``Path.cwd()``; new code should construct an :class:`EmbeddingTier`
    explicitly and call its :meth:`~EmbeddingTier.embed_text` method to
    opt into per-scan-root cache isolation.
    """

    return _get_default_tier().embed_text(text)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-math cosine similarity — no numpy dep requirement.

    Used by :meth:`ClusterStore.find_by_embedding` so that tests can
    exercise the threshold logic even when :mod:`hnswlib` is absent
    (they pass a list of ``(cluster_id, centroid)`` and loop manually).
    Returns ``0.0`` when either vector has zero magnitude.
    """

    if len(a) != len(b):
        raise ValueError("vector length mismatch")
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class HNSWIndex:
    """Thin wrapper around ``hnswlib.Index(space='cosine', dim=EMBEDDING_DIM)``.

    Kept deliberately small; the tier-3 bulk of the dedup logic lives in
    :meth:`ClusterStore.find_by_embedding`, which does a linear-scan
    fallback when :mod:`hnswlib` is absent. ``HNSWIndex`` only exists
    when ``_IMPORT_OK`` — the constructor raises ``RuntimeError``
    otherwise so a miswired caller fails loudly rather than silently
    producing bogus results.
    """

    def __init__(
        self, dim: int = EMBEDDING_DIM, max_elements: int = 10000
    ) -> None:
        if not _IMPORT_OK:
            raise RuntimeError("hnswlib unavailable")
        self._index = hnswlib.Index(space="cosine", dim=dim)
        self._index.init_index(
            max_elements=max_elements, ef_construction=200, M=16
        )
        self._index.set_ef(50)
        self._labels: dict[int, str] = {}
        self._next_label: int = 0
        self._dim = dim

    def add_items(
        self, vectors: list[list[float]], cluster_ids: list[str]
    ) -> None:
        """Append ``vectors`` to the index, each labeled with its cluster id."""

        if len(vectors) != len(cluster_ids):
            raise ValueError("vectors / cluster_ids length mismatch")
        labels: list[int] = []
        for cid in cluster_ids:
            self._labels[self._next_label] = cid
            labels.append(self._next_label)
            self._next_label += 1
        self._index.add_items(vectors, labels)

    def search(self, vec: list[float], k: int = 1) -> list[tuple[str, float]]:
        """Return ``[(cluster_id, distance)]`` of k nearest.

        Distance is hnswlib's cosine distance (``1 - cosine_similarity``).
        Returns an empty list when the index is empty.
        """

        if self._next_label == 0:
            return []
        labels, dists = self._index.knn_query(
            [vec], k=min(k, self._next_label)
        )
        return [
            (self._labels[int(label)], float(dist))
            for label, dist in zip(labels[0], dists[0])
        ]

    def save(self, path: Path) -> None:
        """Persist the index to ``path`` via hnswlib's native serializer."""

        self._index.save_index(str(path))

    def load(self, path: Path, max_elements: int) -> None:
        """Load a previously-saved index from ``path``."""

        self._index.load_index(str(path), max_elements=max_elements)


__all__ = [
    "EMBEDDING_MODEL_NAME",
    "EMBEDDING_DIM",
    "EmbeddingTier",
    "_resolve_embedding_cache_dir",
    "probe_embedding_tier",
    "embed_text",
    "cosine_similarity",
    "HNSWIndex",
]
