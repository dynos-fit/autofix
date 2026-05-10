"""PROACTIVE-05: unknown analyzer names produce a stderr warning.

Before this fix, ``analyze_files(..., analyzers=['ruf'])`` returned
``[]`` with zero stderr output — a typo'd CLI flag silently turned
the scan into a no-op. The warning makes the failure mode visible
and lists the known names so the user can spot the typo.

The same warning fires from ``autofix.funnel.pipeline.run_scan`` for
parity (covered separately by the run_scan tests).
"""
from __future__ import annotations

import io
from contextlib import redirect_stderr
from pathlib import Path

from autofix.analyzers import analyze_files


def test_unknown_name_emits_stderr_warning() -> None:
    """A typo'd analyzer name should produce one warning line on stderr."""
    buf = io.StringIO()
    with redirect_stderr(buf):
        findings = analyze_files(
            [], analyzers=["unknown_xyz"], repo_root=Path("."),
        )
    assert findings == []
    out = buf.getvalue()
    assert "unknown analyzer 'unknown_xyz'" in out
    assert "skipped" in out


def test_known_name_produces_no_warning() -> None:
    """A valid analyzer name with empty file list should NOT warn."""
    buf = io.StringIO()
    with redirect_stderr(buf):
        analyze_files([], analyzers=["cheap"], repo_root=Path("."))
    out = buf.getvalue()
    assert "unknown analyzer" not in out


def test_warning_lists_known_names() -> None:
    """The warning should include the registry's known names so a user can
    spot a typo (e.g. 'ruff' vs 'linter:ruff') immediately.
    """
    buf = io.StringIO()
    with redirect_stderr(buf):
        analyze_files([], analyzers=["typo"], repo_root=Path("."))
    out = buf.getvalue()
    # Either a 'Did you mean' suggestion OR the full list of known names.
    assert "Did you mean" in out or "Known names" in out
    # The list MUST include 'cheap' — it's the canary that proves the
    # registry was consulted (and not just a bare warning).
    assert "cheap" in out


def test_multiple_unknowns_each_produce_a_line() -> None:
    """Two unknown names → two warning lines."""
    buf = io.StringIO()
    with redirect_stderr(buf):
        analyze_files(
            [],
            analyzers=["unknown_a", "unknown_b"],
            repo_root=Path("."),
        )
    out = buf.getvalue()
    assert "unknown_a" in out
    assert "unknown_b" in out
    # Two separate warning lines.
    warning_lines = [line for line in out.splitlines() if "unknown analyzer" in line]
    assert len(warning_lines) == 2


def test_mixed_known_and_unknown_warns_only_for_unknown() -> None:
    """``analyzers=['cheap', 'typo']`` → warning only for 'typo'."""
    buf = io.StringIO()
    with redirect_stderr(buf):
        analyze_files(
            [], analyzers=["cheap", "typo"], repo_root=Path("."),
        )
    out = buf.getvalue()
    assert "'typo'" in out
    assert "'cheap'" not in out
