"""Dedup-specific pytest configuration.

Adds the ``requires_dedup_extras`` marker auto-skip: tests marked
with ``@pytest.mark.requires_dedup_extras`` are skipped when
``autofix_next.dedup.embedding.probe_embedding_tier()`` returns
``(False, ...)`` -- i.e. sentence-transformers / hnswlib are not
installed.
"""
from __future__ import annotations

import pytest


def pytest_collection_modifyitems(config, items):
    from autofix_next.dedup.embedding import probe_embedding_tier

    available, _reason = probe_embedding_tier()
    if available:
        return

    skip_marker = pytest.mark.skip(
        reason="requires_dedup_extras: [dedup] optional deps not installed"
    )
    for item in items:
        if "requires_dedup_extras" in item.keywords:
            item.add_marker(skip_marker)
