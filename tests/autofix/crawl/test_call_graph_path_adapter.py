"""Tests for ``CallGraphPathAdapter`` (path-level wrapper)."""
from __future__ import annotations

from pathlib import Path

from autofix.crawl import CallGraphAdapter
from autofix.crawl._call_graph_adapter import CallGraphPathAdapter


class _MockCallGraph:
    """Minimal mock satisfying the symbol-level call graph shape.

    Tests construct one with explicit ``symbols_in``, ``callers_of``,
    and ``callees_of`` return values so the adapter's behavior is
    pinned to specific inputs.
    """

    def __init__(
        self,
        symbols_by_path: dict[str, set[str]] | None = None,
        callers: dict[frozenset[str], set[str]] | None = None,
        callees: dict[frozenset[str], set[str]] | None = None,
    ) -> None:
        self._sbp = symbols_by_path or {}
        self._callers = callers or {}
        self._callees = callees or {}

    def symbols_in(self, path: str) -> frozenset[str]:
        return frozenset(self._sbp.get(path, set()))

    def callers_of(self, symbol_ids, max_depth: int) -> frozenset[str]:
        key = frozenset(symbol_ids)
        return frozenset(self._callers.get(key, set()))

    def callees_of(self, symbol_ids, max_depth: int) -> frozenset[str]:
        key = frozenset(symbol_ids)
        return frozenset(self._callees.get(key, set()))


def test_satisfies_callgraph_adapter_protocol() -> None:
    adapter = CallGraphPathAdapter(_MockCallGraph())
    assert isinstance(adapter, CallGraphAdapter)


def test_seed_with_no_symbols_returns_empty() -> None:
    """A path the symbol graph doesn't know about → []."""
    cg = _MockCallGraph()  # no symbols anywhere
    adapter = CallGraphPathAdapter(cg)
    assert adapter.neighbors_of(Path("unknown.py")) == []


def test_seed_with_callers_returns_caller_paths() -> None:
    """Symbol in foo.py has a caller in bar.py → bar.py in result."""
    cg = _MockCallGraph(
        symbols_by_path={"foo.py": {"foo.py::do_thing"}},
        callers={frozenset({"foo.py::do_thing"}): {"bar.py::caller"}},
    )
    adapter = CallGraphPathAdapter(cg)
    assert adapter.neighbors_of(Path("foo.py")) == [Path("bar.py")]


def test_seed_with_callees_returns_callee_paths() -> None:
    """Symbol in foo.py calls into baz.py → baz.py in result."""
    cg = _MockCallGraph(
        symbols_by_path={"foo.py": {"foo.py::do_thing"}},
        callees={frozenset({"foo.py::do_thing"}): {"baz.py::helper"}},
    )
    adapter = CallGraphPathAdapter(cg)
    assert adapter.neighbors_of(Path("foo.py")) == [Path("baz.py")]


def test_callers_and_callees_unioned() -> None:
    """Both upstream and downstream paths appear in the result."""
    cg = _MockCallGraph(
        symbols_by_path={"foo.py": {"foo.py::do_thing"}},
        callers={frozenset({"foo.py::do_thing"}): {"upstream.py::caller"}},
        callees={frozenset({"foo.py::do_thing"}): {"downstream.py::callee"}},
    )
    adapter = CallGraphPathAdapter(cg)
    assert adapter.neighbors_of(Path("foo.py")) == [
        Path("downstream.py"),
        Path("upstream.py"),
    ]


def test_overlapping_caller_and_callee_deduped() -> None:
    """A path that's both caller and callee shows up once."""
    cg = _MockCallGraph(
        symbols_by_path={"foo.py": {"foo.py::do_thing"}},
        callers={frozenset({"foo.py::do_thing"}): {"both.py::a"}},
        callees={frozenset({"foo.py::do_thing"}): {"both.py::b"}},
    )
    adapter = CallGraphPathAdapter(cg)
    # Two different symbols both in both.py → dedup to one Path
    assert adapter.neighbors_of(Path("foo.py")) == [Path("both.py")]


def test_seed_path_excluded_from_result() -> None:
    """The seed's own path NEVER appears in its neighbors list,
    even if intra-file edges exist.
    """
    cg = _MockCallGraph(
        symbols_by_path={"foo.py": {"foo.py::a", "foo.py::b"}},
        callers={
            # b calls a within foo.py
            frozenset({"foo.py::a", "foo.py::b"}): {"foo.py::c"},
        },
    )
    adapter = CallGraphPathAdapter(cg)
    assert Path("foo.py") not in adapter.neighbors_of(Path("foo.py"))


def test_result_is_sorted() -> None:
    """Determinism — neighbor list comes back alphabetically sorted."""
    cg = _MockCallGraph(
        symbols_by_path={"foo.py": {"foo.py::do_thing"}},
        callers={
            frozenset({"foo.py::do_thing"}): {
                "z_last.py::x",
                "a_first.py::y",
                "m_middle.py::z",
            },
        },
    )
    adapter = CallGraphPathAdapter(cg)
    result = adapter.neighbors_of(Path("foo.py"))
    assert result == [
        Path("a_first.py"),
        Path("m_middle.py"),
        Path("z_last.py"),
    ]


def test_malformed_symbol_id_skipped_not_raised() -> None:
    """A symbol_id without ``::`` is skipped silently — defensive
    against malformed graph state.
    """
    cg = _MockCallGraph(
        symbols_by_path={"foo.py": {"foo.py::do_thing"}},
        callers={
            frozenset({"foo.py::do_thing"}): {
                "valid.py::ok",
                "no_colons_at_all",  # malformed
            },
        },
    )
    adapter = CallGraphPathAdapter(cg)
    assert adapter.neighbors_of(Path("foo.py")) == [Path("valid.py")]


# ---------------------------------------------------------------------------
# Path normalization (abs ↔ rel) — fixes the production bug where
# picker passes absolute paths but SCIP indexes by relative.
# ---------------------------------------------------------------------------


def test_absolute_input_with_root_returns_absolute(tmp_path: Path) -> None:
    """When ``root`` is provided AND input is absolute, output is absolute."""
    cg = _MockCallGraph(
        symbols_by_path={"foo.py": {"foo.py::do_thing"}},
        callers={frozenset({"foo.py::do_thing"}): {"bar.py::caller"}},
    )
    adapter = CallGraphPathAdapter(cg, root=tmp_path)
    abs_in = tmp_path / "foo.py"

    result = adapter.neighbors_of(abs_in)

    # Absolute in → absolute out, neighbor resolved under root.
    assert result == [tmp_path / "bar.py"]


def test_absolute_input_without_root_falls_through(tmp_path: Path) -> None:
    """No ``root`` + absolute input → SCIP lookup uses absolute string,
    finds nothing (expected — the test mock keys by relative).
    """
    cg = _MockCallGraph(
        symbols_by_path={"foo.py": {"foo.py::do_thing"}},
        callers={frozenset({"foo.py::do_thing"}): {"bar.py::caller"}},
    )
    adapter = CallGraphPathAdapter(cg)  # no root
    abs_in = tmp_path / "foo.py"

    # Mock keys "foo.py" by relative path; absolute string won't match.
    assert adapter.neighbors_of(abs_in) == []


def test_relative_input_unchanged_with_root(tmp_path: Path) -> None:
    """Relative input + root provided → output stays relative."""
    cg = _MockCallGraph(
        symbols_by_path={"foo.py": {"foo.py::do_thing"}},
        callers={frozenset({"foo.py::do_thing"}): {"bar.py::caller"}},
    )
    adapter = CallGraphPathAdapter(cg, root=tmp_path)

    result = adapter.neighbors_of(Path("foo.py"))

    # Relative in → relative out (matches existing test convention).
    assert result == [Path("bar.py")]


# ---------------------------------------------------------------------------
# Text-reference signal (language-agnostic neighbors)
# ---------------------------------------------------------------------------


def test_text_incoming_only_contributes_neighbor() -> None:
    """No SCIP, only incoming text refs → still returns neighbor."""
    adapter = CallGraphPathAdapter(
        call_graph=None,
        text_incoming={"target.dart": frozenset({"caller.dart"})},
    )
    assert adapter.neighbors_of(Path("target.dart")) == [Path("caller.dart")]


def test_text_outgoing_only_contributes_neighbor() -> None:
    """No SCIP, only outgoing text refs → still returns neighbor."""
    adapter = CallGraphPathAdapter(
        call_graph=None,
        text_outgoing={"caller.dart": frozenset({"target.dart"})},
    )
    assert adapter.neighbors_of(Path("caller.dart")) == [Path("target.dart")]


def test_scip_and_text_signals_unioned() -> None:
    """SCIP neighbors + text neighbors → unioned and deduped."""
    cg = _MockCallGraph(
        symbols_by_path={"foo.py": {"foo.py::do_thing"}},
        callers={frozenset({"foo.py::do_thing"}): {"caller.py::a"}},
    )
    adapter = CallGraphPathAdapter(
        call_graph=cg,
        text_incoming={"foo.py": frozenset({"caller.py", "text_only.py"})},
    )

    result = adapter.neighbors_of(Path("foo.py"))

    # caller.py from BOTH SCIP + text → appears once. text_only.py only
    # from text → still included.
    assert result == [Path("caller.py"), Path("text_only.py")]


def test_text_only_mode_no_call_graph() -> None:
    """``call_graph=None`` is supported — text indexes alone work."""
    adapter = CallGraphPathAdapter(
        call_graph=None,
        text_incoming={"a.dart": frozenset({"b.dart"})},
        text_outgoing={"a.dart": frozenset({"c.dart"})},
    )
    result = adapter.neighbors_of(Path("a.dart"))
    assert result == [Path("b.dart"), Path("c.dart")]


def test_text_self_excluded() -> None:
    """Text indexes pointing back at the seed get filtered out."""
    adapter = CallGraphPathAdapter(
        call_graph=None,
        # The seed somehow appears in its own incoming/outgoing —
        # the adapter must filter it.
        text_incoming={"foo.py": frozenset({"foo.py", "real_caller.py"})},
    )
    result = adapter.neighbors_of(Path("foo.py"))
    assert result == [Path("real_caller.py")]
