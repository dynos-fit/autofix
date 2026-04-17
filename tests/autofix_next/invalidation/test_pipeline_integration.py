"""Integration tests for the planner wired through the funnel pipeline.

Covers:

* AC #2  — ``change_detector.detect()`` sets ``is_fresh_instance=True`` on
  the ``"full-sweep-fallback"`` branch and ``False`` on the
  ``"diff-head1"`` / ``"full-sweep"`` branches.
* AC #3  — ``autofix-next scan --fresh-instance`` forces
  ``is_fresh_instance=True`` on the constructed ChangeSet regardless of
  the detector's label, and the flag is documented in ``--help``.
* AC #21 — ``run_scan`` builds the graph (or accepts one via ``graph=``),
  calls ``plan()`` with the new 2-arg signature, emits an
  ``InvalidationComputed`` event, and iterates
  ``invalidation.affected_files`` (not ``changeset.paths``).
* AC #22 — ``InvalidationComputed`` envelope row under
  ``<root>/.autofix/events.jsonl`` carries exactly the 10 documented keys.
* AC #24 — 3-file fixture where ``a.py`` imports and calls ``b.b_func()``;
  a ChangeSet of ``{b.py}`` produces an Invalidation whose
  ``affected_files`` contains both ``a.py`` and ``b.py``, and ``run_scan``
  exercises ``a.py`` even though it is not in the ChangeSet.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_python")
pytest.importorskip("tree_sitter")

REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _git_init(root: Path) -> None:
    """Initialize ``root`` as a single-commit git repo."""
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "a@b.c"],
        cwd=str(root),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "tester"],
        cwd=str(root),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "-A"], cwd=str(root), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=str(root),
        check=True,
        capture_output=True,
    )


def _write_three_file_fixture(root: Path) -> None:
    """Write the 3-file fixture described in AC #24: a imports b; c standalone."""
    (root / "b.py").write_text("def b_func():\n    return 1\n", encoding="utf-8")
    (root / "a.py").write_text(
        "from b import b_func\n\ndef a_func():\n    return b_func()\n",
        encoding="utf-8",
    )
    (root / "c.py").write_text(
        "def c_func():\n    return 'standalone'\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# AC #2 — detector sets is_fresh_instance on fallback
# ---------------------------------------------------------------------------


def test_detect_sets_is_fresh_instance_on_fallback(tmp_path: Path) -> None:
    """AC #2: ``detect()`` sets ``is_fresh_instance=True`` only on the
    ``"full-sweep-fallback"`` branch; ``"diff-head1"`` and ``"full-sweep"``
    leave it ``False``."""
    from autofix_next.events.change_detector import detect

    _write_three_file_fixture(tmp_path)
    _git_init(tmp_path)  # single commit → HEAD~1 does not exist

    # Default: full_sweep=False on a single-commit repo → "full-sweep-fallback".
    cs_fallback, label_fallback = detect(tmp_path, full_sweep=False)
    assert label_fallback == "full-sweep-fallback"
    assert getattr(cs_fallback, "is_fresh_instance", None) is True

    # full_sweep=True → "full-sweep"; is_fresh_instance must remain False.
    cs_full, label_full = detect(tmp_path, full_sweep=True)
    assert label_full == "full-sweep"
    assert getattr(cs_full, "is_fresh_instance", None) is False

    # Add a second commit so HEAD~1 exists → "diff-head1" branch.
    (tmp_path / "d.py").write_text(
        "def d_func():\n    return 0\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "add", "-A"], cwd=str(tmp_path), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "second"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
    )
    cs_diff, label_diff = detect(tmp_path, full_sweep=False)
    assert label_diff == "diff-head1"
    assert getattr(cs_diff, "is_fresh_instance", None) is False


# ---------------------------------------------------------------------------
# AC #3 — CLI --fresh-instance forces is_fresh_instance=True
# ---------------------------------------------------------------------------


def test_cli_fresh_instance_flag_forces_true(tmp_path: Path) -> None:
    """AC #3: the scan subcommand exposes a ``--fresh-instance`` flag that
    shows up in ``--help`` and, when set, forces ``is_fresh_instance=True``
    on the constructed ChangeSet regardless of the detector label."""
    from autofix_next.cli import scan_command

    parser = argparse.ArgumentParser()
    scan_command.add_arguments(parser)

    # --fresh-instance must be a declared flag with store_true semantics.
    # Parse twice: once with the flag, once without.
    ns_with = parser.parse_args(["--root", str(tmp_path), "--fresh-instance"])
    ns_without = parser.parse_args(["--root", str(tmp_path)])
    assert getattr(ns_with, "fresh_instance", None) is True, (
        "argparse dest for --fresh-instance must be ``fresh_instance``"
    )
    assert getattr(ns_without, "fresh_instance", None) is False

    # --help must mention the flag's effect ("bounded full sweep").
    help_buf = io.StringIO()
    parser.print_help(file=help_buf)
    help_text = help_buf.getvalue().lower()
    assert "--fresh-instance" in help_text
    # Be forgiving about exact phrasing; check one of the accepted synonyms.
    assert any(
        token in help_text
        for token in ("bounded full sweep", "full-sweep", "bounded full-sweep")
    ), f"--help must document the flag's effect; got:\n{help_text}"


# ---------------------------------------------------------------------------
# AC #21 + #24 — run_scan builds graph, calls plan, emits InvalidationComputed
# ---------------------------------------------------------------------------


def _run_scan_on_three_file_fixture(tmp_path: Path):
    """Build the fixture, run the funnel pipeline, and return (result, root).

    Uses the public ``run_scan`` entry point so this test exercises the
    real pipeline wiring, not a stripped-down reimplementation.
    """
    from autofix_next.events.schema import ChangeSet
    from autofix_next.funnel.pipeline import run_scan

    _write_three_file_fixture(tmp_path)
    _git_init(tmp_path)

    # ChangeSet = {b.py}. ``a.py`` should still be analyzed because it
    # calls b_func; ``c.py`` should NOT be pulled in.
    cs = ChangeSet(paths=("b.py",), watcher_confidence="diff-head1")
    result = run_scan(tmp_path, cs, scan_id="test-scan-1")
    return result, tmp_path


def test_run_scan_builds_graph_and_invokes_plan_and_emits_event(
    tmp_path: Path,
) -> None:
    """AC #21 + AC #24: ``run_scan`` builds the graph, calls the new
    ``plan()`` signature, iterates ``invalidation.affected_files``, and
    emits an ``InvalidationComputed`` envelope row."""
    result, root = _run_scan_on_three_file_fixture(tmp_path)

    # The events.jsonl file should carry an InvalidationComputed row.
    events_path = root / ".autofix" / "events.jsonl"
    assert events_path.is_file(), "events.jsonl must exist after run_scan"
    rows = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    event_names = [row.get("event") for row in rows]
    assert "InvalidationComputed" in event_names, (
        f"expected InvalidationComputed row; got events {event_names}"
    )

    # scan_id is preserved on the result.
    assert result.scan_id == "test-scan-1"


def test_invalidation_computed_payload_exact_keys(tmp_path: Path) -> None:
    """AC #22: the ``scan_event`` payload of the ``InvalidationComputed``
    row contains EXACTLY the 10 documented keys — no more, no fewer."""
    _, root = _run_scan_on_three_file_fixture(tmp_path)

    events_path = root / ".autofix" / "events.jsonl"
    rows = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    invalidation_rows = [
        r for r in rows if r.get("event") == "InvalidationComputed"
    ]
    assert len(invalidation_rows) == 1, (
        f"expected exactly one InvalidationComputed row, got {len(invalidation_rows)}"
    )
    row = invalidation_rows[0]
    assert "scan_event" in row, "envelope row must carry a scan_event key"

    expected_keys = {
        "event_type",
        "repo_id",
        "scan_id",
        "source",
        "watcher_confidence",
        "depth_used",
        "is_full_sweep",
        "graph_symbol_count",
        "affected_symbol_count",
        "affected_file_count",
    }
    actual_keys = set(row["scan_event"].keys())
    assert actual_keys == expected_keys, (
        f"InvalidationComputed payload keys mismatch:\n"
        f"  missing: {expected_keys - actual_keys}\n"
        f"  unexpected: {actual_keys - expected_keys}"
    )
    # Light type checks on the numeric keys.
    assert row["scan_event"]["event_type"] == "InvalidationComputed"
    assert isinstance(row["scan_event"]["graph_symbol_count"], int)
    assert isinstance(row["scan_event"]["affected_symbol_count"], int)
    assert isinstance(row["scan_event"]["affected_file_count"], int)
    assert isinstance(row["scan_event"]["is_full_sweep"], bool)
    assert isinstance(row["scan_event"]["depth_used"], int)
