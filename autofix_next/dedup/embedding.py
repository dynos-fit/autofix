"""Optional embedding tier (tier 3) — sentence-transformers + hnswlib.

Imports are at module TOP so a missing dep surfaces immediately via
``ImportError``; :func:`probe_embedding_tier` catches that plus model-load
failures and returns a structured ``(bool, reason)`` so
:class:`~autofix_next.dedup.cluster_store.ClusterStore` can fall back to
SimHash-only when the tier is unavailable.

Public surface (AC #25):

* :data:`EMBEDDING_MODEL_NAME` — frozen model id (``all-MiniLM-L6-v2``).
* :data:`EMBEDDING_DIM` — vector dimension (``384``).
* :data:`EMBEDDING_CACHE_DIR` — project-specific model cache directory.
* :func:`probe_embedding_tier` — non-raising capability probe.
* :func:`embed_text` — compute a sentence-transformer embedding.
* :func:`cosine_similarity` — pure-math similarity (no numpy required).
* :class:`HNSWIndex` — thin wrapper around :mod:`hnswlib` for ANN search.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

EMBEDDING_MODEL_NAME: str = "all-MiniLM-L6-v2"
EMBEDDING_DIM: int = 384
EMBEDDING_CACHE_DIR: Path = Path("~/.cache/autofix-next/models/").expanduser()

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

    This probe does NOT trigger a model download. It checks a handful
    of likely cache locations (project-specific ``EMBEDDING_CACHE_DIR``
    first, then sentence-transformers' default under
    ``~/.cache/torch/sentence_transformers/``).
    """

    if not _IMPORT_OK:
        return (False, "deps_missing")
    # Locate the cached model without downloading. sentence-transformers'
    # default cache is ``~/.cache/torch/sentence_transformers/``. Check
    # EMBEDDING_CACHE_DIR first (project-specific), then the default.
    from os import environ

    offline = (
        environ.get("HF_HUB_OFFLINE") == "1"
        or environ.get("TRANSFORMERS_OFFLINE") == "1"
    )
    cached = (EMBEDDING_CACHE_DIR / EMBEDDING_MODEL_NAME).exists() or (
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


_MODEL_CACHE: Any = None


def _load_model() -> Any:
    """Lazily load and memoize the sentence-transformer model.

    Callers must ensure ``_IMPORT_OK`` first — :func:`embed_text` does.
    """

    global _MODEL_CACHE
    if _MODEL_CACHE is None:
        EMBEDDING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _MODEL_CACHE = sentence_transformers.SentenceTransformer(
            EMBEDDING_MODEL_NAME, cache_folder=str(EMBEDDING_CACHE_DIR)
        )
    return _MODEL_CACHE


def embed_text(text: str) -> list[float]:
    """Return an ``EMBEDDING_DIM``-length float list for ``text``.

    Raises :class:`RuntimeError` if the embedding tier is not available
    — callers should gate via :func:`probe_embedding_tier` first.
    """

    if not _IMPORT_OK:
        raise RuntimeError(
            "embedding tier unavailable: deps_missing"
        ) from _IMPORT_ERROR
    model = _load_model()
    vec = model.encode(text, convert_to_numpy=True, show_progress_bar=False)
    return [float(x) for x in vec.tolist()]


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
    "EMBEDDING_CACHE_DIR",
    "probe_embedding_tier",
    "embed_text",
    "cosine_similarity",
    "HNSWIndex",
]
