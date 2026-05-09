"""Public adapter contracts for the crawl subsystem (autofix.crawl.contracts)."""
from __future__ import annotations

from pathlib import Path

from autofix.crawl import CallGraphAdapter, GitLogAdapter


# ---------------------------------------------------------------------------
# GitLogAdapter
# ---------------------------------------------------------------------------


class _GitLogConformingMock:
    """Minimum object that satisfies the GitLogAdapter Protocol."""

    def list_python_files(self) -> list[str]:
        return []

    def days_since_last_commit(self, path: str) -> int:
        return 0

    def commits_in_last_30_days(self, path: str) -> int:
        return 0

    def import_fanout(self, path: str) -> int:
        return 0


class _GitLogMissingOneMethod:
    """Conforming except for ``import_fanout`` — should fail isinstance."""

    def list_python_files(self) -> list[str]:
        return []

    def days_since_last_commit(self, path: str) -> int:
        return 0

    def commits_in_last_30_days(self, path: str) -> int:
        return 0


def test_gitlog_conforming_mock_passes_isinstance() -> None:
    """A mock with all four methods satisfies the Protocol."""
    assert isinstance(_GitLogConformingMock(), GitLogAdapter)


def test_gitlog_missing_method_fails_isinstance() -> None:
    """A mock missing one of the four methods does NOT satisfy the Protocol."""
    assert not isinstance(_GitLogMissingOneMethod(), GitLogAdapter)


def test_gitlog_unrelated_object_fails_isinstance() -> None:
    """A bare object satisfies nothing."""
    assert not isinstance(object(), GitLogAdapter)


def test_gitlog_real_adapter_satisfies_protocol() -> None:
    """The reference _GitLogAdapter from cycle_runner satisfies the Protocol
    at runtime — confirms the contract matches the live implementation.
    """
    from autofix.cli.cycle_runner import _GitLogAdapter

    # The adapter takes a root Path; doesn't matter for isinstance.
    instance = _GitLogAdapter(Path("/tmp"))
    assert isinstance(instance, GitLogAdapter)


# ---------------------------------------------------------------------------
# CallGraphAdapter
# ---------------------------------------------------------------------------


class _CallGraphConformingMock:
    def neighbors_of(self, path: Path) -> list[Path]:
        return []


class _CallGraphMissingMethod:
    pass


def test_callgraph_conforming_mock_passes_isinstance() -> None:
    assert isinstance(_CallGraphConformingMock(), CallGraphAdapter)


def test_callgraph_missing_method_fails_isinstance() -> None:
    assert not isinstance(_CallGraphMissingMethod(), CallGraphAdapter)


def test_callgraph_unrelated_object_fails_isinstance() -> None:
    assert not isinstance(object(), CallGraphAdapter)


def test_no_neighbors_stub_satisfies_protocol() -> None:
    """The fallback stub returned by cycle_runner._build_call_graph on
    CallGraph build failure must satisfy the Protocol.
    """
    from autofix.cli.cycle_runner import _build_call_graph

    # Pass a path that won't be a real git repo — forces fall-soft to
    # the no-op stub. Either way the result must satisfy the Protocol.
    adapter = _build_call_graph(Path("/tmp/__nonexistent_for_test"))
    assert isinstance(adapter, CallGraphAdapter)
    # And the fall-soft adapter returns [] for any input.
    assert adapter.neighbors_of(Path("anything.py")) == []


# ---------------------------------------------------------------------------
# Re-export
# ---------------------------------------------------------------------------


def test_contracts_re_exported_from_crawl_init() -> None:
    """Both Protocols must be importable from ``autofix.crawl`` (the
    curated public API), not just from ``autofix.crawl.contracts``.
    """
    from autofix import crawl

    assert "GitLogAdapter" in crawl.__all__
    assert "CallGraphAdapter" in crawl.__all__
    assert crawl.GitLogAdapter is GitLogAdapter
    assert crawl.CallGraphAdapter is CallGraphAdapter
