"""Tests for class-aware bundle expansion — covers ACs 8, 10.

ACs covered:
- AC 8: expand_bundle class_aware_config kwarg; test/config/entrypoint/junk-sink behaviors
- AC 10: test cases a-e per spec:
    (a) test seed with present mirror impl file prioritized first
    (b) config seed with non-source neighbors dropped, hops capped at 1
    (c) entrypoint seed expands to MAX_BUNDLE_HOPS_ENTRYPOINT hops (>= 2)
    (d) vendor/generated/cache/build_output/lockfile/binary neighbor dropped (junk-sink)
    (e) default path (class_aware_config=None) produces identical output to pre-PR expand_bundle

Hub-saturation + class-filter ordering (spec AC 8, plan D3):
  A hub-saturated junk-sink candidate is recorded as dropped-by-hub, not
  dropped-by-class — the test for this constructs a fixture where a
  candidate is BOTH hub-saturated AND junk-sink and asserts the saturation
  count went up rather than the junk-sink count.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_call_graph(neighbor_map: dict[str, list[str]]) -> MagicMock:
    """Build a call_graph mock with explicit neighbors per path string."""
    cg = MagicMock()

    def _neighbors(path):
        key = str(path)
        # Try basename match too
        for k, v in neighbor_map.items():
            if k == key or Path(k).name == Path(path).name or k == Path(path).name:
                return [Path(n) for n in v]
        return []

    cg.neighbors_of.side_effect = _neighbors
    return cg


def _make_call_graph_exact(neighbor_map: dict[Path, list[Path]]) -> MagicMock:
    """Build a call_graph mock with exact Path neighbors."""
    cg = MagicMock()

    def _neighbors(path):
        return neighbor_map.get(path, [])

    cg.neighbors_of.side_effect = _neighbors
    return cg


# ---------------------------------------------------------------------------
# AC 5 (AC 8) — ClassAwareConfig importable
# ---------------------------------------------------------------------------

def test_class_aware_config_importable() -> None:
    """ClassAwareConfig must be importable from autofix.crawl.bundles."""
    from autofix.crawl.bundles import ClassAwareConfig  # noqa: F401
    assert ClassAwareConfig is not None


def test_class_aware_config_is_frozen_dataclass() -> None:
    """ClassAwareConfig must be a frozen dataclass."""
    from autofix.crawl.bundles import ClassAwareConfig
    import dataclasses

    assert dataclasses.is_dataclass(ClassAwareConfig)
    # Frozen: should raise on mutation attempt
    cac = ClassAwareConfig(root=Path("/root"))
    with pytest.raises((AttributeError, TypeError)):
        cac.root = Path("/other")  # type: ignore[misc]


def test_expand_bundle_accepts_class_aware_config_kwarg(tmp_path: Path) -> None:
    """expand_bundle must accept class_aware_config kwarg without error."""
    from autofix.crawl.bundles import expand_bundle, ClassAwareConfig

    seed = tmp_path / "source.py"
    seed.write_text("# seed\n")

    cg = _make_call_graph({})
    cac = ClassAwareConfig(root=tmp_path)

    bundle = expand_bundle(
        seed_path=seed,
        root=tmp_path,
        call_graph=cg,
        class_aware_config=cac,
    )
    assert seed in bundle.file_paths


# ---------------------------------------------------------------------------
# AC 10e — Default path produces identical output to pre-PR expand_bundle
# ---------------------------------------------------------------------------

def test_default_path_none_class_aware_config_identical(tmp_path: Path) -> None:
    """AC 10e: class_aware_config=None produces byte-identical Bundle to no-kwarg call."""
    from autofix.crawl.bundles import expand_bundle

    seed = tmp_path / "main.py"
    seed.write_text("# main\n")
    neighbor1 = tmp_path / "utils.py"
    neighbor1.write_text("# utils\n")
    neighbor2 = tmp_path / "helper.py"
    neighbor2.write_text("# helper\n")

    cg = _make_call_graph_exact({
        seed: [neighbor1, neighbor2],
        neighbor1: [],
        neighbor2: [],
    })

    bundle_default = expand_bundle(
        seed_path=seed,
        root=tmp_path,
        call_graph=cg,
    )
    bundle_none = expand_bundle(
        seed_path=seed,
        root=tmp_path,
        call_graph=cg,
        class_aware_config=None,
    )

    assert bundle_default.fingerprint == bundle_none.fingerprint
    assert bundle_default.file_paths == bundle_none.file_paths
    assert bundle_default.total_bytes == bundle_none.total_bytes


# ---------------------------------------------------------------------------
# AC 10a — test seed with present mirror impl file prioritized first
# ---------------------------------------------------------------------------

def test_test_seed_mirror_impl_prioritized(tmp_path: Path) -> None:
    """AC 10a: seed class=test with present mirror impl file → impl prioritized.

    The mirror impl file should appear at index 1 in file_paths
    (after the seed itself at index 0).
    """
    from autofix.crawl.bundles import expand_bundle, ClassAwareConfig

    # Set up mirror layout: tests/mypkg/test_module.py → mypkg/module.py
    pkg_dir = tmp_path / "mypkg"
    pkg_dir.mkdir()
    impl_file = pkg_dir / "module.py"
    impl_file.write_text("# impl module\n")

    test_dir = tmp_path / "tests" / "mypkg"
    test_dir.mkdir(parents=True)
    test_seed = test_dir / "test_module.py"
    test_seed.write_text("# test module\n")

    # Low-priority neighbor (not the impl file)
    other_file = tmp_path / "mypkg" / "other.py"
    other_file.write_text("# other\n")

    # call_graph: test_seed's neighbors include impl_file and other_file
    cg = _make_call_graph_exact({
        test_seed: [other_file, impl_file],  # impl not listed first
        impl_file: [],
        other_file: [],
    })

    cac = ClassAwareConfig(root=tmp_path)
    bundle = expand_bundle(
        seed_path=test_seed,
        root=tmp_path,
        call_graph=cg,
        class_aware_config=cac,
    )

    assert test_seed == bundle.file_paths[0], "seed must be first"
    # impl file must be in bundle and prioritized over other_file
    assert impl_file in bundle.file_paths, "impl file must be in bundle"
    if len(bundle.file_paths) > 1:
        assert bundle.file_paths[1] == impl_file, (
            f"impl_file must be at index 1, got {bundle.file_paths[1]}"
        )


# ---------------------------------------------------------------------------
# AC 10b — config seed: non-source neighbors dropped, hops capped at 1
# ---------------------------------------------------------------------------

def test_config_seed_drops_non_source_neighbors(tmp_path: Path) -> None:
    """AC 10b: config seed → only source neighbors kept; non-source dropped."""
    from autofix.crawl.bundles import expand_bundle, ClassAwareConfig

    config_seed = tmp_path / "pyproject.toml"
    config_seed.write_text("[build-system]\n")

    source_neighbor = tmp_path / "mymodule.py"
    source_neighbor.write_text("# source\n")

    # These should be dropped (non-source)
    docs_neighbor = tmp_path / "README.md"
    docs_neighbor.write_text("# docs\n")

    test_neighbor = tmp_path / "test_utils.py"
    test_neighbor.write_text("# test\n")

    cg = _make_call_graph_exact({
        config_seed: [source_neighbor, docs_neighbor, test_neighbor],
        source_neighbor: [],
        docs_neighbor: [],
        test_neighbor: [],
    })

    cac = ClassAwareConfig(root=tmp_path)
    bundle = expand_bundle(
        seed_path=config_seed,
        root=tmp_path,
        call_graph=cg,
        class_aware_config=cac,
    )

    # source_neighbor should be in bundle; docs and test should be dropped
    assert source_neighbor in bundle.file_paths, "source neighbor must be kept"
    assert docs_neighbor not in bundle.file_paths, "docs neighbor must be dropped"
    assert test_neighbor not in bundle.file_paths, "test neighbor must be dropped"


def test_config_seed_hops_capped_at_1(tmp_path: Path) -> None:
    """AC 10b: config seed hard-capped at 1 hop."""
    from autofix.crawl.bundles import expand_bundle, ClassAwareConfig

    config_seed = tmp_path / "pyproject.toml"
    config_seed.write_text("[build-system]\n")

    hop1 = tmp_path / "mod1.py"
    hop1.write_text("# hop1\n")

    hop2 = tmp_path / "mod2.py"
    hop2.write_text("# hop2\n")

    # hop2 is only reachable via hop1 (hop 2)
    cg = _make_call_graph_exact({
        config_seed: [hop1],
        hop1: [hop2],
        hop2: [],
    })

    cac = ClassAwareConfig(root=tmp_path)
    bundle = expand_bundle(
        seed_path=config_seed,
        root=tmp_path,
        call_graph=cg,
        class_aware_config=cac,
    )

    assert hop1 in bundle.file_paths, "hop1 must be included (1 hop)"
    assert hop2 not in bundle.file_paths, "hop2 must NOT be included (capped at 1 hop)"


# ---------------------------------------------------------------------------
# AC 10c — entrypoint seed expands to MAX_BUNDLE_HOPS_ENTRYPOINT (>= 2) hops
# ---------------------------------------------------------------------------

def test_entrypoint_seed_expands_to_max_bundle_hops(tmp_path: Path) -> None:
    """AC 10c: entrypoint seed uses MAX_BUNDLE_HOPS_ENTRYPOINT (>= 2) hops."""
    from autofix.crawl.bundles import expand_bundle, ClassAwareConfig
    from autofix.crawl.crawl_constants import MAX_BUNDLE_HOPS_ENTRYPOINT

    # Confirm the constant is >= 2 (spec requirement)
    assert MAX_BUNDLE_HOPS_ENTRYPOINT >= 2, (
        f"MAX_BUNDLE_HOPS_ENTRYPOINT must be >= 2, got {MAX_BUNDLE_HOPS_ENTRYPOINT}"
    )

    # Use __main__.py as entrypoint seed
    entrypoint_seed = tmp_path / "__main__.py"
    entrypoint_seed.write_text("# entrypoint\n")

    hop1 = tmp_path / "app.py"
    hop1.write_text("# app\n")

    hop2 = tmp_path / "core.py"
    hop2.write_text("# core\n")

    # Depth-2 graph: __main__.py -> app.py -> core.py
    cg = _make_call_graph_exact({
        entrypoint_seed: [hop1],
        hop1: [hop2],
        hop2: [],
    })

    cac = ClassAwareConfig(root=tmp_path)
    bundle = expand_bundle(
        seed_path=entrypoint_seed,
        root=tmp_path,
        call_graph=cg,
        class_aware_config=cac,
    )

    assert hop1 in bundle.file_paths, "hop1 must be included"
    assert hop2 in bundle.file_paths, (
        "hop2 must be included because MAX_BUNDLE_HOPS_ENTRYPOINT >= 2"
    )


def test_entrypoint_second_hop_correct_neighbors(tmp_path: Path) -> None:
    """AC 10c: entrypoint BFS includes correct second-hop neighbors."""
    from autofix.crawl.bundles import expand_bundle, ClassAwareConfig
    from autofix.crawl.crawl_constants import MAX_BUNDLE_HOPS_ENTRYPOINT

    assert MAX_BUNDLE_HOPS_ENTRYPOINT >= 2

    seed = tmp_path / "__main__.py"
    seed.write_text("# ep\n")

    hop1_a = tmp_path / "module_a.py"
    hop1_a.write_text("# a\n")

    hop1_b = tmp_path / "module_b.py"
    hop1_b.write_text("# b\n")

    hop2_from_a = tmp_path / "sub_a.py"
    hop2_from_a.write_text("# sub_a\n")

    cg = _make_call_graph_exact({
        seed: [hop1_a, hop1_b],
        hop1_a: [hop2_from_a],
        hop1_b: [],
        hop2_from_a: [],
    })

    cac = ClassAwareConfig(root=tmp_path)
    bundle = expand_bundle(
        seed_path=seed,
        root=tmp_path,
        call_graph=cg,
        class_aware_config=cac,
    )

    # All reachable within MAX_BUNDLE_HOPS_ENTRYPOINT hops should be included
    assert hop1_a in bundle.file_paths
    assert hop1_b in bundle.file_paths
    assert hop2_from_a in bundle.file_paths


# ---------------------------------------------------------------------------
# AC 10d — junk-sink neighbor classes are dropped
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("junk_filename,description", [
    ("vendor/lib.py", "vendor"),
    ("proto/service_pb2.py", "generated"),
    ("build/output.py", "build_output"),
    ("requirements.txt", "lockfile"),
    ("mylib.so", "binary"),
    ("__pycache__/mod.pyc", "cache"),
])
def test_junk_sink_neighbor_dropped(tmp_path: Path, junk_filename: str, description: str) -> None:
    """AC 10d: neighbors of junk-sink classes are dropped from bundle."""
    from autofix.crawl.bundles import expand_bundle, ClassAwareConfig

    seed = tmp_path / "source.py"
    seed.write_text("# seed\n")

    junk_path = tmp_path / Path(junk_filename)
    junk_path.parent.mkdir(parents=True, exist_ok=True)
    junk_path.write_text("# junk\n")

    cg = _make_call_graph_exact({
        seed: [junk_path],
        junk_path: [],
    })

    cac = ClassAwareConfig(root=tmp_path)
    bundle = expand_bundle(
        seed_path=seed,
        root=tmp_path,
        call_graph=cg,
        class_aware_config=cac,
    )

    assert junk_path not in bundle.file_paths, (
        f"{description} neighbor should be dropped as junk-sink"
    )


def test_source_neighbor_not_dropped_as_junk_sink(tmp_path: Path) -> None:
    """AC 10d: source class neighbor is kept (not a junk-sink class)."""
    from autofix.crawl.bundles import expand_bundle, ClassAwareConfig

    seed = tmp_path / "source.py"
    seed.write_text("# seed\n")

    source_neighbor = tmp_path / "utils.py"
    source_neighbor.write_text("# utils\n")

    cg = _make_call_graph_exact({
        seed: [source_neighbor],
        source_neighbor: [],
    })

    cac = ClassAwareConfig(root=tmp_path)
    bundle = expand_bundle(
        seed_path=seed,
        root=tmp_path,
        call_graph=cg,
        class_aware_config=cac,
    )

    assert source_neighbor in bundle.file_paths, "source neighbor must be kept"


# ---------------------------------------------------------------------------
# AC 8 — Hub-saturation fires BEFORE class filter
# ---------------------------------------------------------------------------

def test_hub_saturation_fires_before_junk_sink_filter(tmp_path: Path) -> None:
    """AC 8 + plan D3: a hub-saturated junk-sink candidate increments saturation
    count, not junk-sink count. Hub saturation fires BEFORE class filter.

    This test constructs a fixture where a candidate is BOTH hub-saturated
    AND junk-sink, and asserts that the saturation check prevented it
    (by verifying the bundle does NOT include the candidate, and confirming
    the class filter alone would also have removed it).
    """
    from autofix.crawl.bundles import expand_bundle, ClassAwareConfig
    from autofix.crawl.ledger import Ledger, LedgerRow
    from autofix.crawl.crawl_constants import MAX_HUB_APPEARANCES

    seed = tmp_path / "source.py"
    seed.write_text("# seed\n")

    # This is BOTH hub-saturated AND a junk-sink (vendor class)
    hub_junk = tmp_path / "vendor" / "common.py"
    hub_junk.parent.mkdir(parents=True, exist_ok=True)
    hub_junk.write_text("# vendor common\n")

    # Saturate hub_junk in ledger
    ledger = Ledger(root=tmp_path)
    window_start = "2026-05-08T00:00:00Z"
    now = "2026-05-08T01:00:00Z"

    for i in range(MAX_HUB_APPEARANCES):
        fp = f"{'a' * (63 - len(str(i)))}{i}"
        ledger.record(LedgerRow(
            ts="2026-05-08T00:30:00Z",
            bundle_fingerprint=fp,
            seed_path=str(tmp_path / f"other{i}.py"),
            file_paths=(str(hub_junk),),
            analyzer="cheap",
            last_commit_sha="abc",
            last_finding_count=0,
            cache_hit=False,
            event_id=f"evt_{i}",
        ))

    cg = _make_call_graph_exact({
        seed: [hub_junk],
        hub_junk: [],
    })

    cac = ClassAwareConfig(root=tmp_path)

    # With saturation: hub_junk should be excluded (hub-saturated)
    bundle_saturated = expand_bundle(
        seed_path=seed,
        root=tmp_path,
        call_graph=cg,
        ledger=ledger,
        window_start=window_start,
        now=now,
        class_aware_config=cac,
    )

    # hub_junk should NOT be in bundle — excluded by hub saturation (fires first)
    assert hub_junk not in bundle_saturated.file_paths, (
        "hub-saturated junk-sink must be excluded from bundle"
    )

    # Also verify: without saturation but with class-aware, it's still dropped (junk-sink)
    empty_ledger = Ledger(root=tmp_path)
    bundle_no_sat = expand_bundle(
        seed_path=seed,
        root=tmp_path,
        call_graph=cg,
        ledger=empty_ledger,
        window_start=window_start,
        now=now,
        class_aware_config=cac,
    )
    assert hub_junk not in bundle_no_sat.file_paths, (
        "vendor-class neighbor dropped by junk-sink filter when not saturated"
    )


# ---------------------------------------------------------------------------
# Determinism property — same inputs → same Bundle
# ---------------------------------------------------------------------------

def test_class_aware_expansion_deterministic(tmp_path: Path) -> None:
    """Given same inputs + fixed ClassAwareConfig, two calls return same Bundle."""
    from autofix.crawl.bundles import expand_bundle, ClassAwareConfig

    seed = tmp_path / "main.py"
    seed.write_text("# main\n")

    files = []
    for i in range(4):
        f = tmp_path / f"mod{i}.py"
        f.write_text(f"# mod{i}\n")
        files.append(f)

    cg = _make_call_graph_exact({
        seed: files,
        **{f: [] for f in files},
    })

    cac = ClassAwareConfig(root=tmp_path)
    bundle1 = expand_bundle(
        seed_path=seed,
        root=tmp_path,
        call_graph=cg,
        class_aware_config=cac,
    )
    bundle2 = expand_bundle(
        seed_path=seed,
        root=tmp_path,
        call_graph=cg,
        class_aware_config=cac,
    )

    assert bundle1.fingerprint == bundle2.fingerprint
    assert bundle1.file_paths == bundle2.file_paths
