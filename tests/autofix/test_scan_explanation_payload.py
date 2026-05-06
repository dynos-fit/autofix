"""AC 53 — ``ScanExplanation`` envelope payload contract.

Runs the funnel pipeline end-to-end against a tiny git repo with a single
unused-import finding and asserts:

* exactly one ``ScanExplanation`` row is emitted per scan;
* the payload carries EXACTLY the enumerated 13 top-level keys (12 data
  fields plus ``event_type``);
* ``llm_verdict_histogram`` is a 3-key dict whose counts sum to the
  number of per-finding :class:`ScheduleDecision` s returned by the
  scan.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


# AC 53 — the exact top-level key set ScanExplanation must emit.
# event_type is the 13th key (count-including-event_type); the spec text
# enumerates 12 data keys plus event_type.
_EXPECTED_KEYS: frozenset[str] = frozenset(
    {
        "event_type",
        "repo_id",
        "scan_id",
        "commit_sha",
        "span_id",
        "trace_id",
        "event_id",
        "diff_match",
        "invalidation_plan_size",
        "analyzer_finding_count",
        "ranking_percentile",
        "dedup_cluster_size",
        "llm_verdict_histogram",
    }
)

_HISTOGRAM_KEYS: frozenset[str] = frozenset({"confirmed", "rejected", "skipped"})


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
def seeded_repo(tmp_path: Path) -> Path:
    """Two-commit repo with exactly one unused-import finding on module_b."""
    _init_git_repo(tmp_path)
    (tmp_path / "module_a.py").write_text(
        "import os\n\npath = os.getcwd()\n", encoding="utf-8"
    )
    _commit(tmp_path, "initial")
    (tmp_path / "module_b.py").write_text(
        "import json  # unused on purpose\n\nx = 1\n", encoding="utf-8"
    )
    _commit(tmp_path, "add unused import")
    # Empty policy file so the suppression/tiered defaults apply.
    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir(parents=True, exist_ok=True)
    (autofix_dir / "autofix-policy.json").write_text(
        json.dumps({}), encoding="utf-8"
    )
    return tmp_path


def _read_events(root: Path) -> list[dict]:
    events_path = root / ".autofix" / "events.jsonl"
    assert events_path.is_file(), f"events.jsonl must exist: {events_path}"
    rows: list[dict] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def _run_scan(seeded_repo: Path, monkeypatch: pytest.MonkeyPatch):
    """Run the funnel pipeline in-process with a faked LLM backend."""
    from autofix.llm_backend import LLMResult

    def _fake_run(prompt, **kwargs):
        return LLMResult(
            returncode=0, stdout='{"decision":"confirmed"}', stderr=""
        )

    monkeypatch.setattr("autofix.llm_backend.run_prompt", _fake_run)

    from autofix.events.change_detector import detect
    from autofix.funnel.pipeline import run_scan

    changeset, _conf = detect(seeded_repo, full_sweep=True)
    return run_scan(seeded_repo, changeset, scan_id="test-scan-001")


def test_exactly_one_scan_explanation_row_per_scan(
    seeded_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 53 (i): exactly one ScanExplanation envelope is appended per scan."""
    _run_scan(seeded_repo, monkeypatch)
    rows = _read_events(seeded_repo)
    scan_expl = [r for r in rows if r.get("event") == "ScanExplanation"]
    assert len(scan_expl) == 1, (
        f"expected exactly 1 ScanExplanation row; got {len(scan_expl)}: "
        f"{[r.get('event') for r in rows]!r}"
    )


def test_scan_explanation_payload_has_exactly_13_pinned_keys(
    seeded_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 53 (ii): the payload's top-level key set is frozen at 13 keys."""
    _run_scan(seeded_repo, monkeypatch)
    rows = _read_events(seeded_repo)
    [row] = [r for r in rows if r.get("event") == "ScanExplanation"]
    payload = row.get("scan_event")
    assert isinstance(payload, dict), f"scan_event must be dict: {row!r}"
    observed = frozenset(payload.keys())
    assert observed == _EXPECTED_KEYS, (
        f"ScanExplanation payload keys mismatch\n"
        f"  missing:  {_EXPECTED_KEYS - observed!r}\n"
        f"  unexpected: {observed - _EXPECTED_KEYS!r}"
    )
    # Tighter contract: exactly 13 keys (event_type + 12 data fields).
    assert len(payload) == 13, f"expected 13 keys, got {len(payload)}"


def test_llm_verdict_histogram_counts_sum_to_decisions(
    seeded_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 53 (iii): llm_verdict_histogram counts sum to len(decisions)."""
    scan_result = _run_scan(seeded_repo, monkeypatch)
    decisions = scan_result.schedule_decisions
    assert len(decisions) >= 1, (
        "seeded_repo guarantees at least one finding + decision"
    )

    rows = _read_events(seeded_repo)
    [row] = [r for r in rows if r.get("event") == "ScanExplanation"]
    hist = row["scan_event"]["llm_verdict_histogram"]
    assert isinstance(hist, dict)
    assert frozenset(hist.keys()) == _HISTOGRAM_KEYS, (
        f"histogram keys must be exactly {_HISTOGRAM_KEYS!r}; got {hist!r}"
    )
    assert all(isinstance(v, int) for v in hist.values()), (
        f"histogram values must be ints; got {hist!r}"
    )
    total = hist["confirmed"] + hist["rejected"] + hist["skipped"]
    assert total == len(decisions), (
        f"histogram sum {total} != len(decisions) {len(decisions)}; "
        f"hist={hist!r}, decisions={[d.decision for d in decisions]!r}"
    )
