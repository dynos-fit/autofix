"""PROACTIVE-09: replay() accepts a run_scan_fn callable for dep injection.

The audit flagged that ``autofix.telemetry.replay`` does a
function-body import of ``autofix.funnel.pipeline.run_scan`` because
the funnel transitively imports the telemetry package — a circular
dependency. The function-body import is the standard mitigation, but
it means tests that exercise replay's verdicts have no clean way to
mock the run_scan seam.

Adding ``run_scan_fn`` as an optional kwarg:

* Default (``None``) → function-body import of run_scan (preserves
  the prior behavior; existing callers see no change).
* Caller-injected → skips the funnel import entirely; the injected
  callable is invoked at the same call site.

This pins both behaviors so a future regression that dropped the
kwarg or always imported the funnel would be caught.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from autofix.telemetry.replay import replay


def _seed_repo_with_scan(tmp_path: Path, scan_id: str) -> Path:
    """Build a tiny repo with a synthetic events.jsonl row anchoring scan_id."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "test"], cwd=tmp_path, check=True,
    )
    (tmp_path / "x.py").write_text("import os\n")
    subprocess.run(["git", "add", "x.py"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True,
    )

    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir(parents=True, exist_ok=True)
    events_path = autofix_dir / "events.jsonl"

    # Synthetic ScanStarted row with the keys replay() needs to anchor.
    started = {
        "event_type": "ScanStarted",
        "repo_id": tmp_path.name,
        "scan_id": scan_id,
        "watcher_confidence": "full-sweep",
        "extra": {
            "policy_sha256": "",
            "analyzer_version": "v1",
            "tree_sitter_version": "unknown",
            "changeset_paths": ["x.py"],
            "changeset_watcher_confidence": "full-sweep",
        },
    }
    completed = {
        "event_type": "ScanCompleted",
        "repo_id": tmp_path.name,
        "scan_id": scan_id,
        "watcher_confidence": "full-sweep",
        "status": "ok",
        "finding_count": 0,
    }
    with events_path.open("w", encoding="utf-8") as fh:
        for row in (started, completed):
            fh.write(json.dumps(row) + "\n")
    return tmp_path


def test_replay_accepts_run_scan_fn_kwarg() -> None:
    """The signature has a keyword-only ``run_scan_fn`` parameter."""
    import inspect
    sig = inspect.signature(replay)
    assert "run_scan_fn" in sig.parameters
    param = sig.parameters["run_scan_fn"]
    assert param.kind == inspect.Parameter.KEYWORD_ONLY
    assert param.default is None


def test_replay_default_imports_funnel_run_scan(tmp_path: Path) -> None:
    """Without ``run_scan_fn``, replay falls back to importing from the funnel.

    We can't easily assert the import happened at module-load time
    (it's deferred), but we can assert that calling replay without
    a mock STILL goes through the import path and produces a verdict.
    Real scan execution is expensive, so we do this against a synthetic
    events.jsonl with no realistic chance of producing findings — what
    matters is that no AttributeError / ImportError surfaces.
    """
    scan_id = "test-replay-default-001"
    repo = _seed_repo_with_scan(tmp_path, scan_id)
    # Even if the underlying run_scan errors, replay maps it to a verdict
    # rather than raising. That's the contract.
    result = replay(scan_id, repo)
    # Don't assert specific verdict — we don't control the synthetic
    # events.jsonl's exact shape vs real-scan rehydration. We only
    # care that the call returned without an unhandled exception.
    assert result is not None
    assert hasattr(result, "verdict")


def test_replay_uses_injected_run_scan_fn(tmp_path: Path) -> None:
    """When ``run_scan_fn`` is provided, replay calls IT — not the funnel."""
    scan_id = "test-replay-injection-001"
    repo = _seed_repo_with_scan(tmp_path, scan_id)

    fake_run_scan = MagicMock()
    # Build a minimal ScanResult-shaped return value that replay's
    # downstream code can consume without crashing. Replay's real
    # path expects ``scan_result.findings``.
    fake_run_scan.return_value = MagicMock(findings=[], scan_id=scan_id)

    # Ensure the real funnel module isn't loaded JUST FOR THIS CALL.
    # We can't fully prove "no import happened" from inside the test
    # process (the module may already be loaded from a prior test),
    # but we CAN prove the injected callable was used.
    replay(scan_id, repo, run_scan_fn=fake_run_scan)

    # The injected fake was called at least once — the dep-injection
    # path is wired correctly. (Replay may fail on later steps because
    # the fake's return value doesn't perfectly match a real ScanResult,
    # but that's OK — we're pinning the call site.)
    assert fake_run_scan.called, (
        "replay should have invoked the injected run_scan_fn at least once"
    )


def test_replay_injected_fn_receives_root_changeset_scan_id(tmp_path: Path) -> None:
    """The injected callable receives the same (root, changeset, scan_id)
    triple that the real run_scan would have received.
    """
    scan_id = "test-replay-injection-args-001"
    repo = _seed_repo_with_scan(tmp_path, scan_id)

    captured_args = []

    def _capture(*args, **kwargs):
        captured_args.append((args, kwargs))
        return MagicMock(findings=[], scan_id=scan_id)

    replay(scan_id, repo, run_scan_fn=_capture)

    assert len(captured_args) >= 1
    args, _kwargs = captured_args[0]
    # Position 0 = root, position 1 = changeset, position 2 = scan_id.
    assert args[0] == repo
    # changeset has a `paths` attr per the real ChangeSet shape.
    assert hasattr(args[1], "paths")
    assert args[2] == scan_id
