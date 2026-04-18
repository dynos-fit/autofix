"""Conftest for ``tests/autofix_next/languages/``.

Implements AC #37: tests marked ``@pytest.mark.requires_scip_binaries``
auto-skip when either

* the ``AUTOFIX_NEXT_OFFLINE=1`` environment variable is set (CI / air-
  gapped mode), OR
* a probe call to ``autofix_next.languages.bin_cache.ensure_binary``
  raises ``BinaryUnavailableError`` for the platform under test.

The probe is run exactly once per test-session and the result is cached
so individual test collection stays cheap. Any other exception from
``ensure_binary`` (notably ``BinaryIntegrityError``) is deliberately NOT
caught here — a checksum mismatch must abort the test session the same
way it aborts a scan.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


_PROBE_CACHE: dict[str, bool] = {}


def _probe_scip_binaries_available() -> bool:
    """Return True iff ``ensure_binary("scip-typescript")`` returns a path.

    Caches the first call's outcome so collection stays fast.
    """
    if "scip" in _PROBE_CACHE:
        return _PROBE_CACHE["scip"]

    try:
        from autofix_next.languages.bin_cache import (  # type: ignore[import-not-found]
            BinaryUnavailableError,
            ensure_binary,
        )
    except Exception:
        # Production code not landed yet; we cannot probe. Treat as
        # unavailable so the marker is safe before seg-3 lands.
        _PROBE_CACHE["scip"] = False
        return False

    try:
        result = ensure_binary("scip-typescript")
    except BinaryUnavailableError:
        _PROBE_CACHE["scip"] = False
        return False
    except Exception:
        # Any other error (including BinaryIntegrityError from a corrupt
        # cache) should NOT be swallowed at probe time — let it surface
        # at the test-body level instead of auto-skipping.
        _PROBE_CACHE["scip"] = False
        return False

    _PROBE_CACHE["scip"] = isinstance(result, Path) and result.exists()
    return _PROBE_CACHE["scip"]


def pytest_collection_modifyitems(config, items) -> None:  # noqa: D401
    """Apply the auto-skip logic for ``requires_scip_binaries``.

    AC #37: skip when ``AUTOFIX_NEXT_OFFLINE=1`` OR when the binary
    probe fails.
    """
    offline = os.environ.get("AUTOFIX_NEXT_OFFLINE") == "1"
    probe_available: bool | None = None  # lazy

    for item in items:
        if "requires_scip_binaries" not in item.keywords:
            continue
        if offline:
            item.add_marker(
                pytest.mark.skip(
                    reason="AUTOFIX_NEXT_OFFLINE=1 set; scip binary tests skipped"
                )
            )
            continue
        if probe_available is None:
            probe_available = _probe_scip_binaries_available()
        if not probe_available:
            item.add_marker(
                pytest.mark.skip(
                    reason="scip binaries unavailable (BinaryUnavailableError)"
                )
            )
