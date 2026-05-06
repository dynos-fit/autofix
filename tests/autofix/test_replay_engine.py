"""AC 54 — Replay engine verdicts.

Covers all four :class:`ScanRun.verdict` outcomes:

* ``match`` — round-trip replay of a freshly-scanned fixture yields
  ``verdict="match"`` with every historical finding paired to its
  reproduced counterpart.
* ``missing_anchor`` — a ``scan_id`` absent from ``events.jsonl`` yields
  ``verdict="missing_anchor"`` (no exception raised).
* ``version_drift`` — each of the three pinned fields
  (``policy_sha256``, ``analyzer_version``, ``tree_sitter_version``)
  independently triggers ``verdict="version_drift"`` with a populated
  ``drift_reason``.
* ``mismatch`` — a perturbed working tree whose findings diverge from
  the historical ones yields ``verdict="mismatch"``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=root, check=True
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=root, check=True
    )


def _commit(root: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2026-04-17T00:00:00Z",
        "GIT_COMMITTER_DATE": "2026-04-17T00:00:00Z",
    }
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", message],
        cwd=root,
        check=True,
        env=env,
    )


@pytest.fixture
def scan_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seeded repo + one completed scan whose events.jsonl carries the anchor."""
    _init_git_repo(tmp_path)
    (tmp_path / "module_a.py").write_text(
        "import os\n\npath = os.getcwd()\n", encoding="utf-8"
    )
    _commit(tmp_path, "initial")
    (tmp_path / "module_b.py").write_text(
        "import json  # unused on purpose\n\nx = 1\n", encoding="utf-8"
    )
    _commit(tmp_path, "add unused import")

    # Empty policy file — its sha256 is stamped into ScanStarted.extra and
    # is what the version-drift check compares against on replay.
    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir(parents=True, exist_ok=True)
    (autofix_dir / "autofix-policy.json").write_text(
        json.dumps({}), encoding="utf-8"
    )

    # Fake run_prompt so the first scan doesn't hit a real LLM.
    from autofix.llm_backend import LLMResult

    def _fake(prompt, **kw):
        return LLMResult(returncode=0, stdout='{"d":"c"}', stderr="")

    monkeypatch.setattr("autofix.llm_backend.run_prompt", _fake)

    # Drive one full scan via the CLI machinery so the ScanStarted anchor's
    # ``extra`` carries changeset_paths / watcher_confidence / all three
    # version-pin fields — which the replay engine needs.
    import argparse
    from autofix.cli.scan_command import add_arguments, run as scan_run

    parser = argparse.ArgumentParser()
    add_arguments(parser)
    args = parser.parse_args(
        ["--root", str(tmp_path), "--full-sweep", "--scan-id", "fixture-001"]
    )
    rc = scan_run(args)
    assert rc == 0, "seeding scan must succeed"
    return tmp_path


def _anchor_for_scan(root: Path, scan_id: str) -> dict:
    """Return the ScanStarted row for ``scan_id``, or raise."""
    events_path = root / ".autofix" / "events.jsonl"
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        payload = row.get("scan_event") or {}
        if (
            row.get("event") == "ScanStarted"
            and payload.get("scan_id") == scan_id
        ):
            return row
    pytest.fail(f"no ScanStarted row for scan_id={scan_id!r}")


def _rewrite_anchor_extra(
    root: Path, scan_id: str, **overrides: str
) -> None:
    """Rewrite the ScanStarted anchor's ``extra`` fields in-place.

    Used to synthesize version-drift conditions by flipping a single pin
    field to a known-wrong value before replay.
    """
    events_path = root / ".autofix" / "events.jsonl"
    lines = events_path.read_text(encoding="utf-8").splitlines()
    new_lines: list[str] = []
    rewrote = False
    for line in lines:
        if not line.strip():
            new_lines.append(line)
            continue
        row = json.loads(line)
        payload = row.get("scan_event") or {}
        if (
            row.get("event") == "ScanStarted"
            and payload.get("scan_id") == scan_id
        ):
            extra = dict(payload.get("extra") or {})
            for k, v in overrides.items():
                extra[k] = v
            payload["extra"] = extra
            row["scan_event"] = payload
            new_lines.append(json.dumps(row, ensure_ascii=False))
            rewrote = True
        else:
            new_lines.append(line)
    assert rewrote, f"no ScanStarted row rewritten for scan_id={scan_id!r}"
    events_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# AC 54(a) — round-trip match
# ---------------------------------------------------------------------------


def test_replay_round_trip_match(
    scan_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 54(a): replaying a fresh scan against the same tree yields match."""
    from autofix.telemetry.replay import replay

    result = replay("fixture-001", scan_repo)
    assert result.verdict == "match", (
        f"expected 'match'; got {result.verdict!r} "
        f"drift_reason={result.drift_reason!r} "
        f"historical_only={result.historical_only!r} "
        f"reproduced_only={result.reproduced_only!r}"
    )
    assert result.historical_findings, "seeded scan must have ≥1 finding"
    assert result.matched, "every historical finding must pair with reproduced"
    assert not result.historical_only
    assert not result.reproduced_only
    assert not result.prompt_prefix_hash_mismatched, (
        f"prompt_prefix_hash mismatches: "
        f"{result.prompt_prefix_hash_mismatched!r}"
    )


# ---------------------------------------------------------------------------
# AC 54(b) — missing_anchor
# ---------------------------------------------------------------------------


def test_replay_missing_anchor_returns_verdict_without_raising(
    scan_repo: Path,
) -> None:
    """AC 54(b): a scan_id absent from events.jsonl → 'missing_anchor'."""
    from autofix.telemetry.replay import replay

    result = replay("nonexistent-scan", scan_repo)
    assert result.verdict == "missing_anchor"
    assert result.commit_sha == ""
    assert result.historical_findings == ()
    assert result.reproduced_findings == ()
    assert result.drift_reason is None


# ---------------------------------------------------------------------------
# AC 54(c) — version_drift across three pinned fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field_name",
    ["policy_sha256", "analyzer_version", "tree_sitter_version"],
    ids=["policy", "analyzer", "tree_sitter"],
)
def test_replay_version_drift_for_each_pinned_field(
    scan_repo: Path, field_name: str
) -> None:
    """AC 54(c): each of the 3 version-pin fields independently triggers
    ``verdict='version_drift'`` with a populated ``drift_reason``."""
    from autofix.telemetry.replay import replay

    # Poison exactly one field at a time with a value the live-side probe
    # will NOT produce. A 64-hex string that is byte-different from the
    # real policy sha; a version string that the module constants never
    # emit.
    bogus = {
        "policy_sha256": "deadbeef" * 8,  # 64 hex chars, not the real sha
        "analyzer_version": "vBOGUS",
        "tree_sitter_version": "vBOGUS",
    }[field_name]

    _rewrite_anchor_extra(scan_repo, "fixture-001", **{field_name: bogus})

    result = replay("fixture-001", scan_repo)
    assert result.verdict == "version_drift", (
        f"field_name={field_name}: expected 'version_drift'; got "
        f"{result.verdict!r} (drift_reason={result.drift_reason!r})"
    )
    assert result.drift_reason is not None
    assert field_name in result.drift_reason, (
        f"drift_reason should mention {field_name!r}; got "
        f"{result.drift_reason!r}"
    )
    # Historical findings must still be carried on drift; reproduced MUST be
    # empty because the engine short-circuits before re-running the pipeline.
    assert result.reproduced_findings == ()


# ---------------------------------------------------------------------------
# AC 54(d) — mismatch when findings diverge
# ---------------------------------------------------------------------------


def test_replay_mismatch_when_findings_diverge(
    scan_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 54(d): perturbing the working tree so the reproduced finding set
    no longer matches the historical one yields ``verdict='mismatch'``."""
    from autofix.telemetry.replay import replay

    # Remove the file that carried the historical unused-import finding.
    # The reproduced pipeline will find zero findings → historical_only
    # non-empty → mismatch.
    (scan_repo / "module_b.py").unlink()

    result = replay("fixture-001", scan_repo)
    assert result.verdict == "mismatch", (
        f"expected 'mismatch'; got {result.verdict!r}"
    )
    assert result.historical_only, (
        "historical_only must be non-empty when reproduced set is smaller"
    )
