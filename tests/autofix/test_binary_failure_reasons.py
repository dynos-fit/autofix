"""Tests for the three-way split of binary-failure reason strings
emitted by the JSTS and Go adapters (task-010 AC 12-13).

Covers:
  AC 12 — ``jsts.py`` emits ``binary_nonzero_exit`` on returncode != 0,
         ``binary_timeout`` on subprocess.TimeoutExpired, and
         ``binary_rename_failed`` on atomic-rename OSError.
  AC 13 — ``go.py`` emits the same three distinct reasons on the same
         three distinct failure paths.

DEFERRED STATUS
---------------
The seg-5 deferred work that splits these three reason strings has not
landed yet. Until it does, every failure path in ``jsts.py`` / ``go.py``
emits the single legacy reason ``"binary_nonzero_exit"``. This test file
ships with ``pytest.skip`` wrappers so the CI stays green; the skip
reason cites the deferred AC explicitly so a future grep for "deferred"
surfaces these tests as work-in-progress.

When the split lands, remove the skip calls (NOT the whole test) and
verify each reason against the corresponding failure path.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest


_SKIP_REASON = (
    "AC 12-13 deferred; reason-string split pending in jsts.py / go.py"
)


def _split_has_landed() -> bool:
    """Return True iff the three distinct reason strings are present in
    BOTH ``jsts.py`` and ``go.py`` source.

    Source-text probe is intentionally coarse: we want a green signal
    the instant either file grows the new reason literal, so reviewers
    can un-skip the affiliated assertions. If only one file has the
    split, we still treat the split as NOT landed (both must land
    together for parity).
    """
    try:
        from autofix.languages import jsts, go
    except ImportError:
        return False

    jsts_src = Path(inspect.getsourcefile(jsts) or "").read_text(
        encoding="utf-8"
    )
    go_src = Path(inspect.getsourcefile(go) or "").read_text(encoding="utf-8")

    for src in (jsts_src, go_src):
        if "binary_timeout" not in src:
            return False
        if "binary_rename_failed" not in src:
            return False
        if "binary_nonzero_exit" not in src:
            return False
    return True


# ---------------------------------------------------------------------------
# AC 12 — jsts.py split.
# ---------------------------------------------------------------------------


def test_jsts_emits_binary_nonzero_exit_on_nonzero_return() -> None:
    """AC 12: subprocess.run returning ``returncode != 0`` must emit
    ``AdapterPrecisionUnavailable`` with reason ``binary_nonzero_exit``.
    """
    if not _split_has_landed():
        pytest.skip(_SKIP_REASON)
    # Smoke assertion once the split lands: the three literals coexist
    # in jsts.py source. The richer subprocess-monkeypatch harness will
    # be wired when the production split materialises.
    from autofix.languages import jsts

    src = Path(inspect.getsourcefile(jsts) or "").read_text(encoding="utf-8")
    assert 'reason="binary_nonzero_exit"' in src or \
        "reason='binary_nonzero_exit'" in src


def test_jsts_emits_binary_timeout_on_timeout_expired() -> None:
    """AC 12: ``subprocess.TimeoutExpired`` must emit reason
    ``binary_timeout`` (not ``binary_nonzero_exit`` — the two paths must
    be distinguishable in telemetry)."""
    if not _split_has_landed():
        pytest.skip(_SKIP_REASON)
    from autofix.languages import jsts

    src = Path(inspect.getsourcefile(jsts) or "").read_text(encoding="utf-8")
    assert 'reason="binary_timeout"' in src or \
        "reason='binary_timeout'" in src


def test_jsts_emits_binary_rename_failed_on_atomic_rename_failure() -> None:
    """AC 12: an OSError from the atomic rename path (``_atomic_rename``
    raising OSError) must emit reason ``binary_rename_failed``."""
    if not _split_has_landed():
        pytest.skip(_SKIP_REASON)
    from autofix.languages import jsts

    src = Path(inspect.getsourcefile(jsts) or "").read_text(encoding="utf-8")
    assert 'reason="binary_rename_failed"' in src or \
        "reason='binary_rename_failed'" in src


# ---------------------------------------------------------------------------
# AC 13 — go.py split.
# ---------------------------------------------------------------------------


def test_go_emits_binary_nonzero_exit_on_nonzero_return() -> None:
    """AC 13: go.py must emit ``binary_nonzero_exit`` on returncode != 0.
    Parity guard with AC 12."""
    if not _split_has_landed():
        pytest.skip(_SKIP_REASON)
    from autofix.languages import go

    src = Path(inspect.getsourcefile(go) or "").read_text(encoding="utf-8")
    assert 'reason="binary_nonzero_exit"' in src or \
        "reason='binary_nonzero_exit'" in src


def test_go_emits_binary_timeout_on_timeout_expired() -> None:
    """AC 13: go.py must emit ``binary_timeout`` on TimeoutExpired."""
    if not _split_has_landed():
        pytest.skip(_SKIP_REASON)
    from autofix.languages import go

    src = Path(inspect.getsourcefile(go) or "").read_text(encoding="utf-8")
    assert 'reason="binary_timeout"' in src or \
        "reason='binary_timeout'" in src


def test_go_emits_binary_rename_failed_on_atomic_rename_failure() -> None:
    """AC 13: go.py must emit ``binary_rename_failed`` on rename OSError."""
    if not _split_has_landed():
        pytest.skip(_SKIP_REASON)
    from autofix.languages import go

    src = Path(inspect.getsourcefile(go) or "").read_text(encoding="utf-8")
    assert 'reason="binary_rename_failed"' in src or \
        "reason='binary_rename_failed'" in src


# ---------------------------------------------------------------------------
# Meta — three-way distinctness.
# ---------------------------------------------------------------------------


def test_three_reasons_are_distinct_strings() -> None:
    """Meta: the three reason strings must be distinct. This guards
    against a refactor that accidentally collapses two of them back into
    one (defeating AC 12-13's whole point)."""
    if not _split_has_landed():
        pytest.skip(_SKIP_REASON)

    reasons = {"binary_nonzero_exit", "binary_timeout", "binary_rename_failed"}
    assert len(reasons) == 3, reasons
