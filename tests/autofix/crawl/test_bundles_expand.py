"""Bundle expansion bounded by hops/files/bytes (ARCH-016 AC-3..5)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def small_repo(tmp_path: Path) -> Path:
    """Build a 4-file repo. seed (a.py) imports b.py and c.py; d.py is unrelated."""
    (tmp_path / "a.py").write_text(
        "from b import foo\nfrom c import bar\n"
        "def main():\n    return foo() + bar()\n"
    )
    (tmp_path / "b.py").write_text("def foo(): return 1\n")
    (tmp_path / "c.py").write_text("def bar(): return 2\n")
    (tmp_path / "d.py").write_text("# unrelated\n")
    return tmp_path


@pytest.fixture
def fake_call_graph() -> MagicMock:
    """A callable mock that, given a seed Path, returns its 1-hop neighbors."""
    cg = MagicMock()

    def _neighbors(seed: Path) -> list[Path]:
        if seed.name == "a.py":
            return [seed.parent / "b.py", seed.parent / "c.py"]
        return []

    cg.neighbors_of.side_effect = _neighbors
    return cg


def test_singleton_when_no_neighbors(small_repo: Path, fake_call_graph) -> None:
    """A seed with zero neighbors yields a 1-file bundle."""
    from autofix.crawl.bundles import expand_bundle

    seed = small_repo / "d.py"
    bundle = expand_bundle(
        seed_path=seed, root=small_repo, call_graph=fake_call_graph
    )
    assert bundle.file_paths == (seed,)
    assert bundle.seed_path == seed


def test_one_hop_includes_neighbors(small_repo: Path, fake_call_graph) -> None:
    from autofix.crawl.bundles import expand_bundle

    seed = small_repo / "a.py"
    bundle = expand_bundle(
        seed_path=seed, root=small_repo, call_graph=fake_call_graph
    )
    paths = {p.name for p in bundle.file_paths}
    assert "a.py" in paths
    assert "b.py" in paths
    assert "c.py" in paths
    assert "d.py" not in paths


def test_max_files_caps_expansion(small_repo: Path) -> None:
    """When the seed has many neighbors, max_files clamps the bundle."""
    from autofix.crawl.bundles import expand_bundle

    # Create 10 extra files; mock the call graph to claim a.py imports all 10.
    extras = []
    for i in range(10):
        p = small_repo / f"x{i}.py"
        p.write_text(f"# extra {i}\n")
        extras.append(p)

    cg = MagicMock()

    def _neighbors(seed: Path) -> list[Path]:
        if seed.name == "a.py":
            return extras
        return []

    cg.neighbors_of.side_effect = _neighbors

    bundle = expand_bundle(
        seed_path=small_repo / "a.py",
        root=small_repo,
        call_graph=cg,
        max_files=3,
    )
    assert len(bundle.file_paths) == 3  # seed + 2 neighbors


def test_max_bytes_caps_expansion(small_repo: Path) -> None:
    """A megafile alone consumes the byte budget; subsequent neighbors drop."""
    from autofix.crawl.bundles import expand_bundle

    # Create one big neighbor.
    big = small_repo / "big.py"
    big.write_text("x = 1\n" * 5000)  # ~30KB

    cg = MagicMock()
    cg.neighbors_of.side_effect = lambda s: [big] if s.name == "a.py" else []

    bundle = expand_bundle(
        seed_path=small_repo / "a.py",
        root=small_repo,
        call_graph=cg,
        max_bytes=10_000,
    )
    # Only the seed fits — big.py exceeds the budget.
    assert len(bundle.file_paths) == 1
    assert bundle.file_paths[0].name == "a.py"


def test_saturation_drops_hub_neighbors(small_repo: Path, fake_call_graph) -> None:
    """A hub neighbor that's saturated must be dropped from expansion."""
    from autofix.crawl.bundles import expand_bundle

    # Mock a ledger where b.py has been in 3 bundles in the last 24h
    # (saturated at MAX_HUB_APPEARANCES=3).
    ledger = MagicMock()
    def _appearance_count(path, window_start, now):
        return 3 if path.name == "b.py" else 0
    ledger.bundle_appearance_count_in_window.side_effect = _appearance_count

    bundle = expand_bundle(
        seed_path=small_repo / "a.py",
        root=small_repo,
        call_graph=fake_call_graph,
        ledger=ledger,
        window_start="2026-05-07T00:00:00Z",
    )
    paths = {p.name for p in bundle.file_paths}
    assert "a.py" in paths       # seed kept
    assert "c.py" in paths       # non-saturated neighbor kept
    assert "b.py" not in paths   # saturated neighbor dropped


def test_saturation_does_not_drop_seed(small_repo: Path) -> None:
    """A hub seed is still picked as seed; saturation only filters neighbors."""
    from autofix.crawl.bundles import expand_bundle

    cg = MagicMock()
    cg.neighbors_of.return_value = []
    ledger = MagicMock()
    # The seed itself is reported as saturated. Expansion must still
    # include it.
    ledger.bundle_appearance_count_in_window.return_value = 99

    bundle = expand_bundle(
        seed_path=small_repo / "a.py",
        root=small_repo,
        call_graph=cg,
        ledger=ledger,
        window_start="2026-05-07T00:00:00Z",
    )
    assert bundle.seed_path == small_repo / "a.py"
    assert small_repo / "a.py" in bundle.file_paths
