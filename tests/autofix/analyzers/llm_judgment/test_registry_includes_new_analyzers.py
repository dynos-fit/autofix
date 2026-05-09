"""Registry round-trip for ARCH-013 analyzers (AC-19, AC-20)."""
from __future__ import annotations


from autofix.analyzers.llm_judgment.dead_code import DeadCodeJudgmentAnalyzer
from autofix.analyzers.llm_judgment.performance import PerformanceJudgmentAnalyzer
from autofix.funnel.pipeline import _ANALYZER_REGISTRY


def test_dead_code_registered() -> None:
    assert "llm:dead-code" in _ANALYZER_REGISTRY


def test_performance_registered() -> None:
    assert "llm:performance" in _ANALYZER_REGISTRY


def test_dead_code_callable_resolves_to_analyze_classmethod() -> None:
    """Registry callable must be the bound .analyze classmethod."""
    callable_obj = _ANALYZER_REGISTRY["llm:dead-code"]
    assert callable(callable_obj)
    # Bound classmethod's __self__ is the class.
    assert getattr(callable_obj, "__self__", None) is DeadCodeJudgmentAnalyzer


def test_performance_callable_resolves_to_analyze_classmethod() -> None:
    callable_obj = _ANALYZER_REGISTRY["llm:performance"]
    assert callable(callable_obj)
    assert getattr(callable_obj, "__self__", None) is PerformanceJudgmentAnalyzer


def test_package_init_imports_both_submodules() -> None:
    """AC-19: importing the package must load dead_code and performance."""
    import autofix.analyzers.llm_judgment as pkg

    assert hasattr(pkg, "dead_code")
    assert hasattr(pkg, "performance")
