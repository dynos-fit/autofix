"""Tests for per-scan embedding cache-dir resolution (task-010 AC 10-11).

Covers:
  AC 10 — ``AUTOFIX_NEXT_EMBEDDING_CACHE_DIR`` beats any policy override.
  AC 10 — ``policy["embedding_tier"]["cache_dir"]`` beats the scan_root
         default.
  AC 10 — The scan_root default is ``scan_root / ".cache/autofix-next/embedding"``.
  AC 11 — ``EmbeddingTier(root, policy)`` resolves ``self._cache_dir``
         at construction time via the same precedence.
  AC 10 — The module-level constant ``EMBEDDING_CACHE_DIR`` is removed.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _emb():
    return pytest.importorskip("autofix_next.dedup.embedding")


# ---------------------------------------------------------------------------
# AC 10 — module-level constant is GONE.
# ---------------------------------------------------------------------------


def test_module_level_embedding_cache_dir_constant_is_removed() -> None:
    """AC 10: the old process-wide ``EMBEDDING_CACHE_DIR`` constant is
    not importable from the module — per-scan resolution is the only
    supported path."""
    emb = _emb()
    assert not hasattr(emb, "EMBEDDING_CACHE_DIR"), (
        "module-level EMBEDDING_CACHE_DIR constant must be removed; "
        "use EmbeddingTier(root, policy).cache_dir instead"
    )
    assert "EMBEDDING_CACHE_DIR" not in getattr(emb, "__all__", []), (
        "EMBEDDING_CACHE_DIR must not appear in __all__"
    )


def test_resolver_helper_is_exported() -> None:
    """AC 10: ``_resolve_embedding_cache_dir(scan_root, policy)`` exists."""
    emb = _emb()
    assert hasattr(emb, "_resolve_embedding_cache_dir")
    assert callable(emb._resolve_embedding_cache_dir)


# ---------------------------------------------------------------------------
# AC 10 — resolution precedence.
# ---------------------------------------------------------------------------


def test_default_resolves_to_scan_root_dot_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 10: with no env var and no policy, the cache dir is
    ``scan_root / ".cache/autofix-next/embedding"``."""
    emb = _emb()
    monkeypatch.delenv("AUTOFIX_NEXT_EMBEDDING_CACHE_DIR", raising=False)

    got = emb._resolve_embedding_cache_dir(tmp_path, None)
    expected = tmp_path / ".cache" / "autofix-next" / "embedding"
    assert got == expected, f"default must be {expected!r}; got {got!r}"


def test_default_with_empty_policy_still_goes_to_scan_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 10: a policy dict lacking the nested ``embedding_tier.cache_dir``
    key falls through to the scan-root default."""
    emb = _emb()
    monkeypatch.delenv("AUTOFIX_NEXT_EMBEDDING_CACHE_DIR", raising=False)

    got = emb._resolve_embedding_cache_dir(tmp_path, {})
    expected = tmp_path / ".cache" / "autofix-next" / "embedding"
    assert got == expected

    # Policy with unrelated keys → still default.
    got2 = emb._resolve_embedding_cache_dir(
        tmp_path, {"some_other_tier": {"cache_dir": "/nope"}}
    )
    assert got2 == expected

    # Policy with the ``embedding_tier`` key but no ``cache_dir`` inside
    # → still default.
    got3 = emb._resolve_embedding_cache_dir(
        tmp_path, {"embedding_tier": {"unrelated": True}}
    )
    assert got3 == expected

    # Policy with an explicit empty-string cache_dir → treated as absent
    # per AC 10 (empty strings fall through to default).
    got4 = emb._resolve_embedding_cache_dir(
        tmp_path, {"embedding_tier": {"cache_dir": ""}}
    )
    assert got4 == expected


def test_policy_override_beats_scan_root_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 10: ``policy["embedding_tier"]["cache_dir"]`` overrides the
    scan-root default when the env var is absent."""
    emb = _emb()
    monkeypatch.delenv("AUTOFIX_NEXT_EMBEDDING_CACHE_DIR", raising=False)

    policy_cache = tmp_path / "explicit-policy-cache"
    got = emb._resolve_embedding_cache_dir(
        tmp_path, {"embedding_tier": {"cache_dir": str(policy_cache)}}
    )
    assert got == policy_cache, (
        f"policy override must win over scan-root default; got {got!r}"
    )
    # The default path must NOT be returned.
    default_path = tmp_path / ".cache" / "autofix-next" / "embedding"
    assert got != default_path


def test_env_var_beats_policy_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 10: ``AUTOFIX_NEXT_EMBEDDING_CACHE_DIR`` wins over a policy
    override. Critical for CI/operator override of a misconfigured
    per-repo policy."""
    emb = _emb()

    env_cache = tmp_path / "env-cache-wins"
    policy_cache = tmp_path / "policy-cache-loses"
    monkeypatch.setenv("AUTOFIX_NEXT_EMBEDDING_CACHE_DIR", str(env_cache))

    got = emb._resolve_embedding_cache_dir(
        tmp_path, {"embedding_tier": {"cache_dir": str(policy_cache)}}
    )
    assert got == env_cache, (
        f"env var must beat policy override; got {got!r} expected {env_cache!r}"
    )


def test_env_var_beats_scan_root_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 10: env var wins even when no policy is supplied."""
    emb = _emb()
    env_cache = tmp_path / "env-only"
    monkeypatch.setenv("AUTOFIX_NEXT_EMBEDDING_CACHE_DIR", str(env_cache))

    got = emb._resolve_embedding_cache_dir(tmp_path, None)
    assert got == env_cache


def test_env_var_expanduser(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC 10: a leading ``~`` in the env var is expanded via
    ``Path.expanduser()`` so operators can write ``~/.cache/...``
    without the path being taken literally."""
    emb = _emb()
    monkeypatch.setenv(
        "AUTOFIX_NEXT_EMBEDDING_CACHE_DIR", "~/autofix-next-emb-test"
    )

    got = emb._resolve_embedding_cache_dir(Path("/nonexistent-scan-root"), None)
    # Must NOT contain a literal ``~``.
    assert "~" not in str(got), f"~ not expanded in {got!r}"
    # Must end with the provided suffix.
    assert got.name == "autofix-next-emb-test"


def test_two_different_scan_roots_yield_distinct_default_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 10 + AC 11: two concurrent scans against different repos do NOT
    share a cache directory under the default resolution. Regression
    guard for the META-003 cross-repo leak."""
    emb = _emb()
    monkeypatch.delenv("AUTOFIX_NEXT_EMBEDDING_CACHE_DIR", raising=False)

    root_a = tmp_path / "repo-a"
    root_b = tmp_path / "repo-b"
    root_a.mkdir()
    root_b.mkdir()

    cache_a = emb._resolve_embedding_cache_dir(root_a, None)
    cache_b = emb._resolve_embedding_cache_dir(root_b, None)

    assert cache_a != cache_b
    assert str(cache_a).startswith(str(root_a))
    assert str(cache_b).startswith(str(root_b))


# ---------------------------------------------------------------------------
# AC 11 — EmbeddingTier owns the resolved cache dir.
# ---------------------------------------------------------------------------


def test_embedding_tier_stores_resolved_cache_dir_at_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 11: ``EmbeddingTier(root, policy)`` resolves once on __init__
    and exposes the result via ``.cache_dir`` / ``._cache_dir``."""
    emb = _emb()
    monkeypatch.delenv("AUTOFIX_NEXT_EMBEDDING_CACHE_DIR", raising=False)

    tier = emb.EmbeddingTier(tmp_path)
    expected = tmp_path / ".cache" / "autofix-next" / "embedding"
    assert tier.cache_dir == expected
    assert tier._cache_dir == expected

    # With a policy override, the tier picks it up at construction.
    policy_path = tmp_path / "policy-tier-cache"
    tier2 = emb.EmbeddingTier(
        tmp_path, {"embedding_tier": {"cache_dir": str(policy_path)}}
    )
    assert tier2.cache_dir == policy_path


def test_embedding_tier_env_var_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 11: EmbeddingTier construction obeys the same
    env > policy > scan_root precedence as the free helper."""
    emb = _emb()
    env_cache = tmp_path / "env-tier-cache"
    policy_cache = tmp_path / "policy-tier-cache"
    monkeypatch.setenv("AUTOFIX_NEXT_EMBEDDING_CACHE_DIR", str(env_cache))

    tier = emb.EmbeddingTier(
        tmp_path, {"embedding_tier": {"cache_dir": str(policy_cache)}}
    )
    assert tier.cache_dir == env_cache


def test_two_tiers_with_different_roots_do_not_share_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 11: per-instance resolution means two tiers built from
    different scan roots own disjoint cache directories — this is the
    whole point of the refactor."""
    emb = _emb()
    monkeypatch.delenv("AUTOFIX_NEXT_EMBEDDING_CACHE_DIR", raising=False)

    root_a = tmp_path / "repo-a"
    root_b = tmp_path / "repo-b"
    root_a.mkdir()
    root_b.mkdir()

    tier_a = emb.EmbeddingTier(root_a)
    tier_b = emb.EmbeddingTier(root_b)

    assert tier_a.cache_dir != tier_b.cache_dir
    assert tier_a.cache_dir.is_relative_to(root_a)
    assert tier_b.cache_dir.is_relative_to(root_b)
