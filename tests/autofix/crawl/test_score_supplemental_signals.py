"""Tests for supplemental scoring signals in score.py — covers ACs 5, 6, 7.

ACs covered:
- AC 5: relevance accepts new optional kwargs (file_class, file_size_bytes, scoring_flags)
        ScoringFlags is importable from score.py
- AC 6: Golden-file regression test — all-flags-off produces exact pre-PR values
- AC 7: low_value_class_penalty, oversize_file_penalty, entrypoint_boost behaviors

Golden values were derived analytically from the existing score.py formula:
  relevance = max(0.0, min(1.0,
      RELEVANCE_WEIGHT_RECENCY * recency
      + RELEVANCE_WEIGHT_CHURN * churn
      + RELEVANCE_WEIGHT_CENTRALITY * centrality))
where RELEVANCE_WEIGHT_RECENCY=0.5, RELEVANCE_WEIGHT_CHURN=0.3,
      RELEVANCE_WEIGHT_CENTRALITY=0.2, RECENCY_DECAY_DAYS=7.0,
      CHURN_CAP_COMMITS=10, CENTRALITY_CAP_FANOUT=10.

Fixture inputs:
  source.py:       days=0, churn=5, fanout=3  -> 0.71
  test_source.py:  days=1, churn=2, fanout=1  -> 0.5134389498750908
  docs/README.md:  days=3, churn=1, fanout=0  -> 0.3557195287655278
  vendor/lib.py:   days=0, churn=5, fanout=3  -> 0.71
  big_file.py:     days=0, churn=5, fanout=3  -> 0.71
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_git_log(file_data: dict[str, dict]) -> MagicMock:
    """Build a mock git_log with deterministic per-file responses.

    file_data maps filename (basename) to {"days": int, "churn": int, "fanout": int}.
    """
    g = MagicMock()
    g.is_empty.return_value = False

    def _days(path):
        return file_data.get(Path(path).name, {}).get("days", 999)

    def _churn(path):
        return file_data.get(Path(path).name, {}).get("churn", 0)

    def _fanout(path):
        return file_data.get(Path(path).name, {}).get("fanout", 0)

    g.days_since_last_commit.side_effect = _days
    g.commits_in_last_30_days.side_effect = _churn
    g.incoming_dependency_count.side_effect = _fanout
    return g


# Synthetic 5-file mock fixture (AC 6 / discovery Q4)
_FIXTURE_FILE_DATA = {
    "source.py":    {"days": 0, "churn": 5, "fanout": 3},
    "test_source.py": {"days": 1, "churn": 2, "fanout": 1},
    "README.md":    {"days": 3, "churn": 1, "fanout": 0},
    "lib.py":       {"days": 0, "churn": 5, "fanout": 3},  # vendor/lib.py basename
    "big_file.py":  {"days": 0, "churn": 5, "fanout": 3},
}


# ---------------------------------------------------------------------------
# AC 5 — ScoringFlags importable from score.py
# ---------------------------------------------------------------------------

def test_scoring_flags_importable() -> None:
    """ScoringFlags must be importable from autofix.crawl.score."""
    from autofix.crawl.score import ScoringFlags  # noqa: F401
    assert ScoringFlags is not None


def test_scoring_flags_is_frozen_dataclass() -> None:
    """ScoringFlags must be a frozen dataclass with three bool fields."""
    from autofix.crawl.score import ScoringFlags
    import dataclasses

    assert dataclasses.is_dataclass(ScoringFlags)
    fields = {f.name: f for f in dataclasses.fields(ScoringFlags)}
    assert "entrypoint_boost" in fields
    assert "low_value_class_penalty" in fields
    assert "oversize_file_penalty" in fields
    # All default False
    sf = ScoringFlags()
    assert sf.entrypoint_boost is False
    assert sf.low_value_class_penalty is False
    assert sf.oversize_file_penalty is False


def test_scoring_flags_frozen() -> None:
    """ScoringFlags must be frozen (immutable)."""
    from autofix.crawl.score import ScoringFlags

    sf = ScoringFlags()
    with pytest.raises((AttributeError, TypeError)):
        sf.entrypoint_boost = True  # type: ignore[misc]


def test_relevance_accepts_new_kwargs() -> None:
    """relevance must accept file_class, file_size_bytes, scoring_flags kwargs."""
    from autofix.crawl.score import relevance, ScoringFlags
    from autofix.crawl.file_classifier import FileClass

    git_log = _make_git_log(_FIXTURE_FILE_DATA)
    root = Path("/fake/root")

    # Should not raise with new kwargs
    result = relevance(
        Path("source.py"),
        root=root,
        git_log=git_log,
        file_class=FileClass.source,
        file_size_bytes=1000,
        scoring_flags=ScoringFlags(),
    )
    assert isinstance(result, float)
    assert 0.0 <= result <= 1.0


# ---------------------------------------------------------------------------
# AC 6 — Golden-file regression test (all-flags-off exact values)
# ---------------------------------------------------------------------------

class TestGoldenFileRegressionAllFlagsOff:
    """Exact float assertions for the pre-PR formula (AC 6).

    These values are computed analytically and must be reproducible
    by any executor who runs the suite. They may NOT be derived from
    the autofix-standalone commit history.
    """

    def setup_method(self) -> None:
        self.git_log = _make_git_log(_FIXTURE_FILE_DATA)
        self.root = Path("/synthetic/root")

    def _rel(self, path_str: str) -> float:
        from autofix.crawl.score import relevance
        return relevance(Path(path_str), root=self.root, git_log=self.git_log)

    def test_source_py_all_flags_omitted(self) -> None:
        """source.py: days=0,churn=5,fanout=3 → 0.71 (exact)."""
        result = self._rel("source.py")
        assert result == 0.71, f"Expected 0.71, got {result!r}"

    def test_test_source_py_all_flags_omitted(self) -> None:
        """test_source.py: days=1,churn=2,fanout=1 → exact float."""
        import math as _math
        expected = _math.exp(-1 / 7.0)  # recency
        expected = max(0.0, min(1.0, expected))
        churn = min(1.0, 2 / 10)
        centrality = min(1.0, 1 / 10)
        expected_score = max(0.0, min(1.0, 0.5 * expected + 0.3 * churn + 0.2 * centrality))
        result = self._rel("test_source.py")
        assert result == expected_score, f"Expected {expected_score!r}, got {result!r}"

    def test_readme_all_flags_omitted(self) -> None:
        """docs/README.md basename=README.md: days=3,churn=1,fanout=0 → exact float."""
        import math as _math
        recency = max(0.0, min(1.0, _math.exp(-3 / 7.0)))
        churn = min(1.0, 1 / 10)
        centrality = min(1.0, 0 / 10)
        expected = max(0.0, min(1.0, 0.5 * recency + 0.3 * churn + 0.2 * centrality))
        result = self._rel("README.md")
        assert result == expected, f"Expected {expected!r}, got {result!r}"

    def test_vendor_lib_py_all_flags_omitted(self) -> None:
        """vendor/lib.py: days=0,churn=5,fanout=3 → 0.71."""
        result = self._rel("lib.py")
        assert result == 0.71, f"Expected 0.71, got {result!r}"

    def test_big_file_all_flags_omitted(self) -> None:
        """big_file.py: days=0,churn=5,fanout=3 → 0.71."""
        result = self._rel("big_file.py")
        assert result == 0.71, f"Expected 0.71, got {result!r}"

    def test_all_flags_false_scoring_flags_same_as_omitted(self) -> None:
        """Passing ScoringFlags() (all False) must return identical value to omitting flags."""
        from autofix.crawl.score import relevance, ScoringFlags
        from autofix.crawl.file_classifier import FileClass

        base = relevance(Path("source.py"), root=self.root, git_log=self.git_log)
        with_flags = relevance(
            Path("source.py"),
            root=self.root,
            git_log=self.git_log,
            file_class=FileClass.source,
            file_size_bytes=1000,
            scoring_flags=ScoringFlags(
                entrypoint_boost=False,
                low_value_class_penalty=False,
                oversize_file_penalty=False,
            ),
        )
        assert base == with_flags, (
            f"Flags-off must give identical output: base={base!r}, with_flags={with_flags!r}"
        )

    def test_none_scoring_flags_same_as_omitted(self) -> None:
        """Passing scoring_flags=None must return identical value to omitting."""
        from autofix.crawl.score import relevance

        base = relevance(Path("source.py"), root=self.root, git_log=self.git_log)
        with_none = relevance(
            Path("source.py"),
            root=self.root,
            git_log=self.git_log,
            scoring_flags=None,
        )
        assert base == with_none


# ---------------------------------------------------------------------------
# AC 7 — low_value_class_penalty behavior
# ---------------------------------------------------------------------------

class TestLowValueClassPenalty:
    """When low_value_class_penalty=True and file_class is a low-value class,
    relevance multiplies base by LOW_VALUE_CLASS_PENALTY before entrypoint step.
    """

    LOW_VALUE_CLASSES = ["docs", "lockfile", "vendor", "generated", "build_output", "cache", "binary"]

    def test_docs_class_penalty_applied(self) -> None:
        """docs class gets multiplied by LOW_VALUE_CLASS_PENALTY."""
        from autofix.crawl.score import relevance, ScoringFlags
        from autofix.crawl.file_classifier import FileClass
        from autofix.crawl.crawl_constants import LOW_VALUE_CLASS_PENALTY

        git_log = _make_git_log(_FIXTURE_FILE_DATA)
        root = Path("/root")
        flags = ScoringFlags(low_value_class_penalty=True)

        base = relevance(Path("README.md"), root=root, git_log=git_log)
        penalized = relevance(
            Path("README.md"),
            root=root,
            git_log=git_log,
            file_class=FileClass.docs,
            scoring_flags=flags,
        )
        expected = max(0.0, min(1.0, base * LOW_VALUE_CLASS_PENALTY))
        assert penalized == expected, f"Expected {expected!r}, got {penalized!r}"

    def test_vendor_class_penalty_applied(self) -> None:
        """vendor class gets multiplied by LOW_VALUE_CLASS_PENALTY."""
        from autofix.crawl.score import relevance, ScoringFlags
        from autofix.crawl.file_classifier import FileClass
        from autofix.crawl.crawl_constants import LOW_VALUE_CLASS_PENALTY

        git_log = _make_git_log(_FIXTURE_FILE_DATA)
        root = Path("/root")
        flags = ScoringFlags(low_value_class_penalty=True)

        base = relevance(Path("lib.py"), root=root, git_log=git_log)
        penalized = relevance(
            Path("lib.py"),
            root=root,
            git_log=git_log,
            file_class=FileClass.vendor,
            scoring_flags=flags,
        )
        expected = max(0.0, min(1.0, base * LOW_VALUE_CLASS_PENALTY))
        assert penalized == pytest.approx(expected, abs=1e-12)

    def test_generated_class_penalty_applied(self) -> None:
        """generated class gets penalized."""
        from autofix.crawl.score import relevance, ScoringFlags
        from autofix.crawl.file_classifier import FileClass

        git_log = _make_git_log({"source.py": {"days": 0, "churn": 5, "fanout": 3}})
        root = Path("/root")
        flags = ScoringFlags(low_value_class_penalty=True)

        base = relevance(Path("source.py"), root=root, git_log=git_log)
        penalized = relevance(
            Path("source.py"),
            root=root,
            git_log=git_log,
            file_class=FileClass.generated,
            scoring_flags=flags,
        )
        assert penalized < base

    def test_source_class_not_penalized(self) -> None:
        """source class is NOT a low-value class — no penalty applied."""
        from autofix.crawl.score import relevance, ScoringFlags
        from autofix.crawl.file_classifier import FileClass

        git_log = _make_git_log({"source.py": {"days": 0, "churn": 5, "fanout": 3}})
        root = Path("/root")
        flags = ScoringFlags(low_value_class_penalty=True)

        base = relevance(Path("source.py"), root=root, git_log=git_log)
        with_class = relevance(
            Path("source.py"),
            root=root,
            git_log=git_log,
            file_class=FileClass.source,
            scoring_flags=flags,
        )
        assert base == with_class, "source class must not be penalized"

    def test_test_class_not_penalized(self) -> None:
        """test class is NOT a low-value class."""
        from autofix.crawl.score import relevance, ScoringFlags
        from autofix.crawl.file_classifier import FileClass

        git_log = _make_git_log({"test_source.py": {"days": 1, "churn": 2, "fanout": 1}})
        root = Path("/root")
        flags = ScoringFlags(low_value_class_penalty=True)

        base = relevance(Path("test_source.py"), root=root, git_log=git_log)
        with_class = relevance(
            Path("test_source.py"),
            root=root,
            git_log=git_log,
            file_class=FileClass.test,
            scoring_flags=flags,
        )
        assert base == with_class

    @pytest.mark.parametrize("fc_name", LOW_VALUE_CLASSES)
    def test_all_low_value_classes_penalized(self, fc_name: str) -> None:
        """All low-value class values produce a lower score than base."""
        from autofix.crawl.score import relevance, ScoringFlags
        from autofix.crawl.file_classifier import FileClass

        git_log = _make_git_log({"source.py": {"days": 0, "churn": 5, "fanout": 3}})
        root = Path("/root")
        flags = ScoringFlags(low_value_class_penalty=True)
        fc = FileClass(fc_name)

        base = relevance(Path("source.py"), root=root, git_log=git_log)
        penalized = relevance(
            Path("source.py"),
            root=root,
            git_log=git_log,
            file_class=fc,
            scoring_flags=flags,
        )
        assert penalized < base, f"{fc_name} should be penalized but got {penalized} >= base {base}"


# ---------------------------------------------------------------------------
# AC 7 — oversize_file_penalty behavior
# ---------------------------------------------------------------------------

class TestOversizeFilePenalty:
    """When oversize_file_penalty=True and file_size_bytes > MAX_RELEVANT_FILE_BYTES,
    relevance multiplies by OVERSIZE_FILE_PENALTY.
    """

    def test_oversize_file_gets_penalty(self) -> None:
        """file_size_bytes > MAX_RELEVANT_FILE_BYTES → OVERSIZE_FILE_PENALTY applied."""
        from autofix.crawl.score import relevance, ScoringFlags
        from autofix.crawl.crawl_constants import MAX_RELEVANT_FILE_BYTES, OVERSIZE_FILE_PENALTY

        git_log = _make_git_log({"big_file.py": {"days": 0, "churn": 5, "fanout": 3}})
        root = Path("/root")
        flags = ScoringFlags(oversize_file_penalty=True)

        base = relevance(Path("big_file.py"), root=root, git_log=git_log)
        penalized = relevance(
            Path("big_file.py"),
            root=root,
            git_log=git_log,
            file_size_bytes=MAX_RELEVANT_FILE_BYTES + 1,
            scoring_flags=flags,
        )
        expected = max(0.0, min(1.0, base * OVERSIZE_FILE_PENALTY))
        assert penalized == pytest.approx(expected, abs=1e-12)

    def test_normal_sized_file_not_penalized(self) -> None:
        """file_size_bytes <= MAX_RELEVANT_FILE_BYTES → no penalty."""
        from autofix.crawl.score import relevance, ScoringFlags
        from autofix.crawl.crawl_constants import MAX_RELEVANT_FILE_BYTES

        git_log = _make_git_log({"source.py": {"days": 0, "churn": 5, "fanout": 3}})
        root = Path("/root")
        flags = ScoringFlags(oversize_file_penalty=True)

        base = relevance(Path("source.py"), root=root, git_log=git_log)
        with_size = relevance(
            Path("source.py"),
            root=root,
            git_log=git_log,
            file_size_bytes=MAX_RELEVANT_FILE_BYTES,  # exactly at threshold
            scoring_flags=flags,
        )
        assert base == with_size, "At-threshold file must not be penalized"

    def test_exact_threshold_not_penalized(self) -> None:
        """Exactly at MAX_RELEVANT_FILE_BYTES is not penalized (> not >=)."""
        from autofix.crawl.score import relevance, ScoringFlags
        from autofix.crawl.crawl_constants import MAX_RELEVANT_FILE_BYTES

        git_log = _make_git_log({"source.py": {"days": 0, "churn": 5, "fanout": 3}})
        root = Path("/root")
        flags = ScoringFlags(oversize_file_penalty=True)

        base = relevance(Path("source.py"), root=root, git_log=git_log)
        exact = relevance(
            Path("source.py"),
            root=root,
            git_log=git_log,
            file_size_bytes=MAX_RELEVANT_FILE_BYTES,
            scoring_flags=flags,
        )
        assert base == exact

    def test_file_size_none_no_penalty(self) -> None:
        """file_size_bytes=None → no oversize penalty even if flag is True."""
        from autofix.crawl.score import relevance, ScoringFlags

        git_log = _make_git_log({"source.py": {"days": 0, "churn": 5, "fanout": 3}})
        root = Path("/root")
        flags = ScoringFlags(oversize_file_penalty=True)

        base = relevance(Path("source.py"), root=root, git_log=git_log)
        with_none_size = relevance(
            Path("source.py"),
            root=root,
            git_log=git_log,
            file_size_bytes=None,
            scoring_flags=flags,
        )
        assert base == with_none_size


# ---------------------------------------------------------------------------
# AC 7 — entrypoint_boost behavior
# ---------------------------------------------------------------------------

class TestEntrypointBoost:
    """When entrypoint_boost=True and file_class == FileClass.entrypoint,
    relevance adds ENTRYPOINT_BOOST to base before clamp.
    """

    def test_entrypoint_class_gets_boost(self) -> None:
        """entrypoint class gets ENTRYPOINT_BOOST added."""
        from autofix.crawl.score import relevance, ScoringFlags
        from autofix.crawl.file_classifier import FileClass
        from autofix.crawl.crawl_constants import ENTRYPOINT_BOOST

        git_log = _make_git_log({"source.py": {"days": 0, "churn": 5, "fanout": 3}})
        root = Path("/root")
        flags = ScoringFlags(entrypoint_boost=True)

        base = relevance(Path("source.py"), root=root, git_log=git_log)
        boosted = relevance(
            Path("source.py"),
            root=root,
            git_log=git_log,
            file_class=FileClass.entrypoint,
            scoring_flags=flags,
        )
        expected = max(0.0, min(1.0, base + ENTRYPOINT_BOOST))
        assert boosted == pytest.approx(expected, abs=1e-12)

    def test_non_entrypoint_class_no_boost(self) -> None:
        """Non-entrypoint class does NOT get entrypoint boost."""
        from autofix.crawl.score import relevance, ScoringFlags
        from autofix.crawl.file_classifier import FileClass

        git_log = _make_git_log({"source.py": {"days": 0, "churn": 5, "fanout": 3}})
        root = Path("/root")
        flags = ScoringFlags(entrypoint_boost=True)

        base = relevance(Path("source.py"), root=root, git_log=git_log)
        with_source_class = relevance(
            Path("source.py"),
            root=root,
            git_log=git_log,
            file_class=FileClass.source,
            scoring_flags=flags,
        )
        assert base == with_source_class, "source class must not get entrypoint boost"

    def test_entrypoint_boost_clamped_to_1(self) -> None:
        """Entrypoint boost cannot push score above 1.0 after clamp."""
        from autofix.crawl.score import relevance, ScoringFlags
        from autofix.crawl.file_classifier import FileClass

        # Very high base score file
        git_log = _make_git_log({"source.py": {"days": 0, "churn": 10, "fanout": 10}})
        root = Path("/root")
        flags = ScoringFlags(entrypoint_boost=True)

        boosted = relevance(
            Path("source.py"),
            root=root,
            git_log=git_log,
            file_class=FileClass.entrypoint,
            scoring_flags=flags,
        )
        assert boosted <= 1.0


# ---------------------------------------------------------------------------
# AC 7 — order of operations (class penalty → oversize penalty → entrypoint boost → clamp)
# ---------------------------------------------------------------------------

class TestScoringFlagsOrderOfOperations:
    """Verify the order: base → class penalty → oversize penalty → entrypoint boost → clamp."""

    def test_combined_flags_order_of_operations(self) -> None:
        """Apply class penalty then oversize penalty; verify combined effect."""
        from autofix.crawl.score import relevance, ScoringFlags
        from autofix.crawl.file_classifier import FileClass
        from autofix.crawl.crawl_constants import (
            LOW_VALUE_CLASS_PENALTY,
            OVERSIZE_FILE_PENALTY,
            MAX_RELEVANT_FILE_BYTES,
        )

        git_log = _make_git_log({"source.py": {"days": 0, "churn": 5, "fanout": 3}})
        root = Path("/root")
        # Vendor class + oversize
        flags = ScoringFlags(low_value_class_penalty=True, oversize_file_penalty=True)

        base = relevance(Path("source.py"), root=root, git_log=git_log)
        # Order: base → class_penalty → oversize_penalty → clamp
        expected = max(0.0, min(1.0,
            base * LOW_VALUE_CLASS_PENALTY * OVERSIZE_FILE_PENALTY
        ))
        result = relevance(
            Path("source.py"),
            root=root,
            git_log=git_log,
            file_class=FileClass.vendor,
            file_size_bytes=MAX_RELEVANT_FILE_BYTES + 1,
            scoring_flags=flags,
        )
        assert result == pytest.approx(expected, abs=1e-12)

    def test_entrypoint_boost_after_class_penalty(self) -> None:
        """For entrypoint files: class penalty (if any) then boost."""
        from autofix.crawl.score import relevance, ScoringFlags
        from autofix.crawl.file_classifier import FileClass
        from autofix.crawl.crawl_constants import ENTRYPOINT_BOOST

        git_log = _make_git_log({"source.py": {"days": 0, "churn": 5, "fanout": 3}})
        root = Path("/root")
        # entrypoint class is NOT a low-value class, so penalty doesn't apply
        flags = ScoringFlags(low_value_class_penalty=True, entrypoint_boost=True)

        base = relevance(Path("source.py"), root=root, git_log=git_log)
        # entrypoint: no class penalty (it's not a low-value class), then add boost
        expected = max(0.0, min(1.0, base + ENTRYPOINT_BOOST))
        result = relevance(
            Path("source.py"),
            root=root,
            git_log=git_log,
            file_class=FileClass.entrypoint,
            scoring_flags=flags,
        )
        assert result == pytest.approx(expected, abs=1e-12)
