"""Constants-shape tests for the crawl subsystem (ARCH-016 AC-2)."""
from __future__ import annotations



def test_module_imports() -> None:
    """The constants module must import without side effects."""
    from autofix.crawl import crawl_constants  # noqa: F401


def test_horizon_and_thresholds() -> None:
    from autofix.crawl import crawl_constants as c

    assert c.STALENESS_HORIZON_HOURS == 24
    assert c.HUB_SATURATION_WINDOW_HOURS == 24
    assert c.MAX_HUB_APPEARANCES == 3


def test_bundle_expansion_caps() -> None:
    from autofix.crawl import crawl_constants as c

    assert c.MAX_BUNDLE_HOPS == 1
    assert c.MAX_BUNDLE_FILES == 5
    assert c.MAX_BUNDLE_BYTES == 50_000


def test_relevance_weights_sum_to_one() -> None:
    from autofix.crawl import crawl_constants as c

    total = (
        c.RELEVANCE_WEIGHT_RECENCY
        + c.RELEVANCE_WEIGHT_CHURN
        + c.RELEVANCE_WEIGHT_CENTRALITY
    )
    assert abs(total - 1.0) < 1e-9


def test_relevance_weight_proportions() -> None:
    from autofix.crawl import crawl_constants as c

    assert c.RELEVANCE_WEIGHT_RECENCY == 0.5
    assert c.RELEVANCE_WEIGHT_CHURN == 0.3
    assert c.RELEVANCE_WEIGHT_CENTRALITY == 0.2


def test_budget_tier_shape() -> None:
    from autofix.crawl import crawl_constants as c

    for tier in (c.BUDGET_CHEAP, c.BUDGET_BALANCED, c.BUDGET_AGGRESSIVE):
        assert isinstance(tier, dict)
        assert "bundles_per_cycle" in tier
        assert "interval_seconds" in tier
        assert "analyzers" in tier
        assert isinstance(tier["bundles_per_cycle"], int)
        assert isinstance(tier["interval_seconds"], int)
        assert isinstance(tier["analyzers"], tuple)
        assert tier["bundles_per_cycle"] > 0
        assert tier["interval_seconds"] > 0
        assert len(tier["analyzers"]) >= 1


def test_budget_tiers_monotonic() -> None:
    """Budget tiers must scale with their names: cheap < balanced < aggressive."""
    from autofix.crawl import crawl_constants as c

    assert (
        c.BUDGET_CHEAP["bundles_per_cycle"]
        < c.BUDGET_BALANCED["bundles_per_cycle"]
        < c.BUDGET_AGGRESSIVE["bundles_per_cycle"]
    )
    # Aggressive runs more often (smaller interval).
    assert (
        c.BUDGET_AGGRESSIVE["interval_seconds"]
        < c.BUDGET_BALANCED["interval_seconds"]
        < c.BUDGET_CHEAP["interval_seconds"]
    )


def test_budget_cheap_includes_only_cheap_and_security() -> None:
    from autofix.crawl import crawl_constants as c

    assert c.BUDGET_CHEAP["analyzers"] == ("cheap", "llm:security")


def test_budget_aggressive_includes_all_llm_analyzers() -> None:
    from autofix.crawl import crawl_constants as c

    assert c.BUDGET_AGGRESSIVE["analyzers"] == (
        "cheap",
        "llm:security",
        "llm:code-quality",
        "llm:dead-code",
        "llm:performance",
    )


def test_ledger_filename() -> None:
    from autofix.crawl import crawl_constants as c

    assert c.LEDGER_FILENAME == "crawl-ledger.jsonl"


def test_config_keys() -> None:
    from autofix.crawl import crawl_constants as c

    assert c.CONFIG_KEY_MODE == "mode"
    assert c.CONFIG_KEY_BUDGET == "budget"
    assert c.CONFIG_VERSION == 1


def test_mode_values() -> None:
    from autofix.crawl import crawl_constants as c

    assert c.MODE_PREVIEW == "preview"
    assert c.MODE_COMMIT == "commit"
    assert c.MODE_PR == "pr"


def test_all_exports() -> None:
    from autofix.crawl import crawl_constants as c

    expected = {
        "STALENESS_HORIZON_HOURS",
        "HUB_SATURATION_WINDOW_HOURS",
        "MAX_HUB_APPEARANCES",
        "MAX_BUNDLE_HOPS",
        "MAX_BUNDLE_FILES",
        "MAX_BUNDLE_BYTES",
        "RELEVANCE_WEIGHT_RECENCY",
        "RELEVANCE_WEIGHT_CHURN",
        "RELEVANCE_WEIGHT_CENTRALITY",
        "NON_GIT_FALLBACK_SCORE",
        "RECENCY_DECAY_DAYS",
        "CHURN_CAP_COMMITS",
        "CENTRALITY_CAP_FANOUT",
        "BUDGET_CHEAP",
        "BUDGET_BALANCED",
        "BUDGET_AGGRESSIVE",
        "LEDGER_FILENAME",
        "CONFIG_KEY_MODE",
        "CONFIG_KEY_BUDGET",
        "CONFIG_VERSION",
        "MODE_PREVIEW",
        "MODE_COMMIT",
        "MODE_PR",
    }
    assert set(c.__all__) == expected
