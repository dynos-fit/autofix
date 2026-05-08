"""Picker determinism (ARCH-016 AC-15)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock



def _seed_repo(tmp_path: Path, n: int = 8) -> Path:
    for i in range(n):
        (tmp_path / f"f{i}.py").write_text(f"# file {i}\n")
    return tmp_path


def _git_log_with_relevance(file_relevances: dict[str, dict]) -> MagicMock:
    """Build a mock git_log whose per-file responses are deterministic."""
    g = MagicMock()
    g.is_empty.return_value = False

    def _days(path):
        return file_relevances.get(str(path).split("/")[-1], {}).get("days", 999)
    def _churn(path):
        return file_relevances.get(str(path).split("/")[-1], {}).get("churn", 0)
    def _fanout(path):
        return file_relevances.get(str(path).split("/")[-1], {}).get("fanout", 0)
    def _list_files(*_a, **_k):
        return [f for f in file_relevances]

    g.days_since_last_commit.side_effect = _days
    g.commits_in_last_30_days.side_effect = _churn
    g.import_fanout.side_effect = _fanout
    g.list_python_files.side_effect = _list_files
    return g


def test_picker_deterministic_same_inputs(tmp_path: Path) -> None:
    """Two calls with identical inputs must return the same bundle ordering."""
    from autofix.crawl.ledger import Ledger
    from autofix.crawl.picker import pick_next_batch

    repo = _seed_repo(tmp_path, n=6)
    ledger = Ledger(root=repo)
    git_log = _git_log_with_relevance({
        f"f{i}.py": {"days": i, "churn": 10 - i, "fanout": 5}
        for i in range(6)
    })

    cg = MagicMock()
    cg.neighbors_of.return_value = []

    batch1 = pick_next_batch(
        root=repo, ledger=ledger,
        current_commit_sha="abc",
        git_log=git_log, call_graph=cg,
        analyzers=["cheap"],
        bundles_per_cycle=3,
    )
    batch2 = pick_next_batch(
        root=repo, ledger=ledger,
        current_commit_sha="abc",
        git_log=git_log, call_graph=cg,
        analyzers=["cheap"],
        bundles_per_cycle=3,
    )

    fps1 = [b.fingerprint for b, _ in batch1]
    fps2 = [b.fingerprint for b, _ in batch2]
    assert fps1 == fps2


def test_picker_emits_one_pair_per_analyzer(tmp_path: Path) -> None:
    """Each picked bundle yields one (bundle, analyzer) pair per analyzer."""
    from autofix.crawl.ledger import Ledger
    from autofix.crawl.picker import pick_next_batch

    repo = _seed_repo(tmp_path, n=3)
    ledger = Ledger(root=repo)
    git_log = _git_log_with_relevance({
        f"f{i}.py": {"days": i, "churn": 10 - i, "fanout": 5}
        for i in range(3)
    })
    cg = MagicMock()
    cg.neighbors_of.return_value = []

    batch = pick_next_batch(
        root=repo, ledger=ledger,
        current_commit_sha="abc",
        git_log=git_log, call_graph=cg,
        analyzers=["cheap", "llm:security"],
        bundles_per_cycle=2,
    )
    # 2 bundles × 2 analyzers = 4 pairs
    assert len(batch) == 4
    by_fp = {}
    for b, a in batch:
        by_fp.setdefault(b.fingerprint, set()).add(a)
    for analyzers in by_fp.values():
        assert analyzers == {"cheap", "llm:security"}


def test_picker_respects_bundles_per_cycle_cap(tmp_path: Path) -> None:
    from autofix.crawl.ledger import Ledger
    from autofix.crawl.picker import pick_next_batch

    repo = _seed_repo(tmp_path, n=20)
    ledger = Ledger(root=repo)
    git_log = _git_log_with_relevance({
        f"f{i}.py": {"days": i, "churn": 1, "fanout": 1}
        for i in range(20)
    })
    cg = MagicMock()
    cg.neighbors_of.return_value = []

    batch = pick_next_batch(
        root=repo, ledger=ledger,
        current_commit_sha="abc",
        git_log=git_log, call_graph=cg,
        analyzers=["cheap"],
        bundles_per_cycle=4,
    )
    assert len(batch) == 4


def test_picker_prefers_high_freshness_after_seen(tmp_path: Path) -> None:
    """A seen-but-stale file ranks AHEAD of a seen-and-fresh file."""
    from autofix.crawl.ledger import Ledger, LedgerRow
    from autofix.crawl.picker import pick_next_batch

    repo = _seed_repo(tmp_path, n=2)
    ledger = Ledger(root=repo)
    # Both files seen, but f0 was scanned long ago AND its commit_sha
    # has drifted. f1 was scanned just now with current commit.
    ledger.record(LedgerRow(
        ts="2026-05-01T00:00:00Z",
        bundle_fingerprint="0" * 64,
        seed_path="f0.py", file_paths=("f0.py",),
        analyzer="cheap",
        last_commit_sha="OLD", last_finding_count=0,
        cache_hit=False, event_id="e1",
    ))
    ledger.record(LedgerRow(
        ts="2026-05-08T00:00:00Z",
        bundle_fingerprint="1" * 64,
        seed_path="f1.py", file_paths=("f1.py",),
        analyzer="cheap",
        last_commit_sha="NEW", last_finding_count=0,
        cache_hit=False, event_id="e2",
    ))

    git_log = _git_log_with_relevance({
        "f0.py": {"days": 0, "churn": 5, "fanout": 5},
        "f1.py": {"days": 0, "churn": 5, "fanout": 5},
    })
    cg = MagicMock()
    cg.neighbors_of.return_value = []

    batch = pick_next_batch(
        root=repo, ledger=ledger,
        current_commit_sha="NEW",
        git_log=git_log, call_graph=cg,
        analyzers=["cheap"],
        bundles_per_cycle=1,
    )
    assert len(batch) == 1
    bundle, _ = batch[0]
    # f0's commit_sha drifted (OLD vs NEW) → freshness=1.0; f1's didn't → low.
    assert "f0.py" in {p.name for p in bundle.file_paths}
