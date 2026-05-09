"""End-to-end saturation: the picker must drop hub neighbors (ARCH-016 AC-5)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

def _seed_repo(tmp_path: Path, files: list[str]) -> Path:
    for f in files:
        (tmp_path / f).write_text(f"# file {f}\n")
    return tmp_path

def test_saturated_hub_dropped_from_neighbors(tmp_path: Path) -> None:
    """A hub neighbor that has appeared in 3+ bundles in 24h must NOT
    be expanded into the next bundle."""
    from autofix.crawl.ledger import Ledger, LedgerRow
    from autofix.crawl.picker import pick_next_batch

    repo = _seed_repo(tmp_path, ["seed.py", "hub.py", "x.py"])

    ledger = Ledger(root=repo)
    # Saturate hub.py: 3 bundle-records in the last 24h.
    for i in range(3):
        ledger.record(LedgerRow(
            ts=f"2026-05-08T0{i}:00:00Z",
            bundle_fingerprint=f"{i}" * 64,
            seed_path=f"other{i}.py",
            file_paths=(f"other{i}.py", "hub.py"),
            analyzer="cheap",
            last_commit_sha="abc", last_finding_count=0,
            cache_hit=False, event_id=f"e{i}",
        ))

    # git_log says seed.py is the most relevant (recent + churned).
    git_log = MagicMock()
    git_log.is_empty.return_value = False
    relevances = {
        "seed.py": {"days": 0, "churn": 10},
        "hub.py": {"days": 0, "churn": 0},
        "x.py": {"days": 50, "churn": 0},
    }
    git_log.days_since_last_commit.side_effect = (
        lambda p: relevances.get(Path(p).name, {}).get("days", 999)
    )
    git_log.commits_in_last_30_days.side_effect = (
        lambda p: relevances.get(Path(p).name, {}).get("churn", 0)
    )
    git_log.list_candidate_files.return_value = ["seed.py", "hub.py", "x.py"]

    # call graph: seed.py imports hub.py and x.py.
    cg = MagicMock()
    def _neighbors(seed):
        if seed.name == "seed.py":
            return [repo / "hub.py", repo / "x.py"]
        return []
    cg.neighbors_of.side_effect = _neighbors

    batch = pick_next_batch(
        root=repo, ledger=ledger,
        current_commit_sha="def",  # commit drift forces freshness=1.0 for seed.py
        git_log=git_log, call_graph=cg,
        analyzers=["cheap"],
        bundles_per_cycle=1,
        now="2026-05-08T03:00:00Z",  # within 24h of all 3 ledger rows
    )
    assert len(batch) == 1
    bundle, _ = batch[0]
    file_names = {p.name for p in bundle.file_paths}
    assert "seed.py" in file_names
    assert "x.py" in file_names              # non-saturated neighbor kept
    assert "hub.py" not in file_names         # saturated neighbor dropped

def test_saturated_hub_can_still_be_seed(tmp_path: Path) -> None:
    """Saturation only filters neighbors, not seeds. A hot hub still gets
    picked as a seed when its own freshness × relevance is highest."""
    from autofix.crawl.ledger import Ledger, LedgerRow
    from autofix.crawl.picker import pick_next_batch

    repo = _seed_repo(tmp_path, ["hub.py", "x.py"])

    ledger = Ledger(root=repo)
    # Saturate hub.py.
    for i in range(5):
        ledger.record(LedgerRow(
            ts=f"2026-05-08T0{i}:00:00Z",
            bundle_fingerprint=f"{i}" * 64,
            seed_path=f"other{i}.py",
            file_paths=(f"other{i}.py", "hub.py"),
            analyzer="cheap",
            last_commit_sha="abc", last_finding_count=0,
            cache_hit=False, event_id=f"e{i}",
        ))

    git_log = MagicMock()
    git_log.is_empty.return_value = False
    git_log.days_since_last_commit.side_effect = (
        lambda p: 0 if Path(p).name == "hub.py" else 100
    )
    git_log.commits_in_last_30_days.side_effect = (
        lambda p: 50 if Path(p).name == "hub.py" else 0
    )
    git_log.list_candidate_files.return_value = ["hub.py", "x.py"]

    cg = MagicMock()
    cg.neighbors_of.return_value = []

    batch = pick_next_batch(
        root=repo, ledger=ledger,
        current_commit_sha="def",
        git_log=git_log, call_graph=cg,
        analyzers=["cheap"],
        bundles_per_cycle=1,
        now="2026-05-08T06:00:00Z",
    )
    assert len(batch) == 1
    bundle, _ = batch[0]
    # hub.py is the bundle's seed even though it's saturated as a neighbor.
    assert bundle.seed_path.name == "hub.py"
