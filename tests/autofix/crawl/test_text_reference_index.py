"""Tests for the language-agnostic text-reference indexer."""
from __future__ import annotations

from pathlib import Path

from autofix.crawl._text_reference_index import (
    MAX_INDEXED_BYTES,
    build_text_reference_indexes,
)


def test_empty_candidates(tmp_path: Path) -> None:
    """Empty input produces empty indexes (no crash)."""
    incoming, outgoing = build_text_reference_indexes(tmp_path, [])
    assert incoming == {}
    assert outgoing == {}


def test_simple_one_way_reference(tmp_path: Path) -> None:
    """a.dart mentions b.dart → b in incoming, a in outgoing."""
    a = tmp_path / "a.dart"
    b = tmp_path / "b.dart"
    a.write_text("// references b.dart somewhere\n")
    b.write_text("// no references\n")

    incoming, outgoing = build_text_reference_indexes(tmp_path, [a, b])

    assert "b.dart" in incoming
    assert "a.dart" in incoming["b.dart"]
    assert "a.dart" in outgoing
    assert "b.dart" in outgoing["a.dart"]


def test_self_reference_skipped(tmp_path: Path) -> None:
    """A file mentioning its own basename is not its own neighbor."""
    a = tmp_path / "self.dart"
    a.write_text("// docstring talks about self.dart\n")

    incoming, outgoing = build_text_reference_indexes(tmp_path, [a])

    assert incoming.get("self.dart", frozenset()) == frozenset()
    assert outgoing.get("self.dart", frozenset()) == frozenset()


def test_word_boundary_no_false_partial_match(tmp_path: Path) -> None:
    """Mentioning ``foo`` should NOT match the basename ``foo.py``.

    The full basename including extension is required at a word boundary.
    """
    foo = tmp_path / "foo.py"
    ref = tmp_path / "ref.py"
    foo.write_text("# pure code\n")
    ref.write_text("# this mentions foo but never foo dot py\n")

    incoming, outgoing = build_text_reference_indexes(tmp_path, [foo, ref])

    assert "foo.py" not in incoming
    assert "ref.py" not in outgoing


def test_dart_html_cross_reference(tmp_path: Path) -> None:
    """Real-world: a Dart widget references its HTML template."""
    dart = tmp_path / "widget.dart"
    html = tmp_path / "widget.html"
    css = tmp_path / "widget.css"
    dart.write_text(
        "import 'widget.html';\n"
        "import 'widget.css';\n"
        "class Widget {}\n"
    )
    html.write_text("<div>hello</div>\n")
    css.write_text(".widget { color: red; }\n")

    incoming, outgoing = build_text_reference_indexes(
        tmp_path, [dart, html, css]
    )

    # widget.dart's outgoing should include both html and css
    assert {"widget.html", "widget.css"} <= outgoing.get(
        "widget.dart", frozenset()
    )
    # widget.html's incoming should include widget.dart
    assert "widget.dart" in incoming.get("widget.html", frozenset())


def test_bidirectional_reference(tmp_path: Path) -> None:
    """Both files referencing each other → both edges populated."""
    a = tmp_path / "a.go"
    b = tmp_path / "b.go"
    a.write_text("// see b.go for details\n")
    b.write_text("// implementation in a.go\n")

    incoming, outgoing = build_text_reference_indexes(tmp_path, [a, b])

    assert "a.go" in incoming and "b.go" in incoming["a.go"]
    assert "b.go" in incoming and "a.go" in incoming["b.go"]
    assert "b.go" in outgoing.get("a.go", frozenset())
    assert "a.go" in outgoing.get("b.go", frozenset())


def test_oversize_file_capped(tmp_path: Path) -> None:
    """Content past MAX_INDEXED_BYTES is not scanned."""
    big = tmp_path / "big.txt"
    target = tmp_path / "target.txt"
    # Filler past the cap, then the reference at the end.
    big.write_text(("x" * (MAX_INDEXED_BYTES + 100)) + "target.txt\n")
    target.write_text("# target\n")

    incoming, _ = build_text_reference_indexes(tmp_path, [big, target])

    # The reference is past the cap → should NOT be detected.
    assert "target.txt" not in incoming or "big.txt" not in incoming.get(
        "target.txt", frozenset()
    )


def test_unreadable_file_skipped(tmp_path: Path) -> None:
    """A path that can't be read is skipped silently."""
    a = tmp_path / "ghost.txt"
    b = tmp_path / "real.txt"
    b.write_text("references ghost.txt\n")
    # Don't create ghost.txt — read will raise OSError, indexer skips.

    incoming, outgoing = build_text_reference_indexes(tmp_path, [a, b])

    # Real file's outgoing was processed normally.
    assert "real.txt" in outgoing
    # ghost.txt's outgoing wasn't (file didn't exist).
    assert "ghost.txt" not in outgoing


def test_multiple_files_same_basename(tmp_path: Path) -> None:
    """Two files share a basename — ref.py's outgoing resolves to both."""
    nested = tmp_path / "nested"
    nested.mkdir()
    a1 = tmp_path / "config.toml"
    a2 = nested / "config.toml"
    ref = tmp_path / "ref.py"
    a1.write_text("# top-level\n")
    a2.write_text("# nested\n")
    ref.write_text("# loads config.toml at startup\n")

    incoming, outgoing = build_text_reference_indexes(
        tmp_path, [a1, a2, ref]
    )

    # incoming["config.toml"] = files that *mention* config.toml → just ref.py.
    assert incoming.get("config.toml", frozenset()) == frozenset({"ref.py"})
    # outgoing["ref.py"] = files ref.py mentions → both config.toml paths.
    assert {
        "config.toml",
        str(Path("nested") / "config.toml"),
    } <= outgoing.get("ref.py", frozenset())


def test_path_outside_root_skipped(tmp_path: Path) -> None:
    """A candidate path outside root is skipped (relative_to ValueError)."""
    inside = tmp_path / "inside.py"
    inside.write_text("# normal\n")
    outside = Path("/tmp/something_outside_root.py")

    incoming, outgoing = build_text_reference_indexes(
        tmp_path, [inside, outside]
    )

    # Outside path produced no entries; inside path didn't reference outside.
    assert all(
        str(outside) not in v
        for v in list(incoming.values()) + list(outgoing.values())
    )
