"""Integration tests for the post-task-006 ``_analyze_one_file`` seam
(task-006 AC #30, #31, #36, #45).

Coverage
--------
* AC #31 — byte-identical ``CandidateFinding`` list for a ``.py`` input
  from the fixture tree. The reference output comes from running the
  pre-task-006 analyzer chain (``parse_file`` → ``build_symbol_table`` →
  ``analyze``) directly against the same file.
* AC #30 / #45 — ``.ts`` and ``.go`` inputs route through their adapters
  (``parse_cheap`` is called for side effect only) and return ``[]`` —
  because no per-language analyzer is registered, they produce zero
  findings.
* AC #30 — unknown extensions (``.rs``) return ``[]`` without error.
* AC #30 — missing files on disk return ``[]`` silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_ROOT = (
    Path(__file__).resolve().parent / "fixtures" / "mixed_repo"
)


def _import_pipeline():
    return pytest.importorskip("autofix_next.funnel.pipeline")


# ---------------------------------------------------------------------------
# AC #31 — Python byte-identical output
# ---------------------------------------------------------------------------


def test_analyze_one_file_python_byte_identical(tmp_path: Path) -> None:
    """AC #31: post-task-006 ``_analyze_one_file`` returns exactly the
    same ``CandidateFinding`` list for a ``.py`` input as the direct
    pre-task-006 chain.

    The fixture ``py/main.py`` has one unused import
    (``import unused_module``) so the reference output is one finding
    with ``rule_id == 'unused-import.intra-file'`` and
    ``symbol_name == 'unused_module'``.
    """
    pytest.importorskip("tree_sitter_python")
    pytest.importorskip("tree_sitter")
    pipeline = _import_pipeline()

    # Copy the fixture into tmp_path so _analyze_one_file's root/relpath
    # semantics are clean.
    src = (FIXTURE_ROOT / "py" / "main.py").read_text(encoding="utf-8")
    (tmp_path / "main.py").write_text(src, encoding="utf-8")

    # Reference output — run the analyzer chain directly.
    from autofix_next.analyzers.cheap.unused_import import analyze as _analyze
    from autofix_next.indexing.symbols import build_symbol_table
    from autofix_next.parsing.tree_sitter import parse_file

    parse_result = parse_file(tmp_path / "main.py", repo_root=tmp_path)
    symbol_table = build_symbol_table(parse_result)
    reference = _analyze(parse_result, symbol_table)

    # Sanity: the fixture must produce exactly one finding. If this fails
    # the fixture has drifted out of sync with the test contract.
    assert len(reference) == 1, (
        f"fixture py/main.py must produce exactly one unused-import finding, "
        f"got {len(reference)}: {reference!r}"
    )
    assert reference[0].rule_id == "unused-import.intra-file"
    assert reference[0].symbol_name == "unused_module"

    # Post-task-006 path.
    actual = pipeline._analyze_one_file(tmp_path, "main.py")

    assert len(actual) == len(reference), (
        f"_analyze_one_file must return {len(reference)} findings, "
        f"got {len(actual)}"
    )
    for a, r in zip(actual, reference):
        assert a.rule_id == r.rule_id
        assert a.symbol_name == r.symbol_name
        assert a.finding_id == r.finding_id, (
            "finding_id fingerprint must be byte-identical across paths"
        )
        assert a.normalized_import == r.normalized_import
        assert a.start_line == r.start_line
        assert a.end_line == r.end_line


# ---------------------------------------------------------------------------
# AC #30 / #45 — .ts file routes through adapter, returns []
# ---------------------------------------------------------------------------


def test_analyze_one_file_ts_returns_empty(tmp_path: Path) -> None:
    """AC #30 / #45: ``.ts`` input routes through ``JSTSAdapter.parse_cheap``
    for side effect only and returns ``[]`` (no JS/TS analyzer registered)."""
    pipeline = _import_pipeline()

    src = (FIXTURE_ROOT / "ts" / "index.ts").read_text(encoding="utf-8")
    (tmp_path / "index.ts").write_text(src, encoding="utf-8")

    result = pipeline._analyze_one_file(tmp_path, "index.ts")
    assert result == [], (
        f"_analyze_one_file(.ts) must return [], got {result!r}"
    )


# ---------------------------------------------------------------------------
# AC #30 / #45 — .go file routes through adapter, returns []
# ---------------------------------------------------------------------------


def test_analyze_one_file_go_returns_empty(tmp_path: Path) -> None:
    """AC #30 / #45: ``.go`` input routes through ``GoAdapter.parse_cheap``
    for side effect only and returns ``[]``."""
    pipeline = _import_pipeline()

    src = (FIXTURE_ROOT / "go" / "cmd" / "app" / "main.go").read_text(
        encoding="utf-8"
    )
    (tmp_path / "main.go").write_text(src, encoding="utf-8")

    result = pipeline._analyze_one_file(tmp_path, "main.go")
    assert result == [], (
        f"_analyze_one_file(.go) must return [], got {result!r}"
    )


# ---------------------------------------------------------------------------
# AC #30 — unknown extension returns []
# ---------------------------------------------------------------------------


def test_analyze_one_file_unknown_extension_returns_empty(tmp_path: Path) -> None:
    """AC #30: ``.rs`` (or any extension not in the six registered) returns
    ``[]`` silently — no warning, no error."""
    pipeline = _import_pipeline()

    (tmp_path / "lib.rs").write_text("fn main() {}\n", encoding="utf-8")

    result = pipeline._analyze_one_file(tmp_path, "lib.rs")
    assert result == [], (
        f"_analyze_one_file(.rs) must return [], got {result!r}"
    )


# ---------------------------------------------------------------------------
# AC #30 — missing file returns []
# ---------------------------------------------------------------------------


def test_analyze_one_file_missing_file_returns_empty(tmp_path: Path) -> None:
    """AC #30: a ``relpath`` that does not exist on disk returns ``[]``
    silently (pre-existing tolerance preserved post-refactor)."""
    pipeline = _import_pipeline()

    # No file written — lookup will fail target.is_file().
    result = pipeline._analyze_one_file(tmp_path, "does_not_exist.py")
    assert result == [], (
        f"_analyze_one_file(missing) must return [], got {result!r}"
    )


# ---------------------------------------------------------------------------
# AC #30 — non-python adapter parse_cheap errors are swallowed
# ---------------------------------------------------------------------------


def test_analyze_one_file_swallows_parse_cheap_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #30: if a non-python adapter's ``parse_cheap`` raises
    ``FileNotFoundError`` / ``PermissionError`` / ``OSError``, the helper
    still returns ``[]`` — these are per-file IO errors, not scan-
    stopping bugs.
    """
    pipeline = _import_pipeline()

    # Write a .ts file to disk so target.is_file() succeeds and routing
    # proceeds into the adapter.parse_cheap call.
    (tmp_path / "broken.ts").write_text("const x = 1;\n", encoding="utf-8")

    # Replace the adapter bound under the .ts extension with a stub
    # whose parse_cheap raises OSError. We do this by patching
    # lookup_by_extension in the pipeline's import binding.
    from autofix_next import languages

    class _BoomAdapter:
        language = "typescript"
        extensions = (".ts",)

        def parse_cheap(self, source):  # noqa: ANN001 — stub
            raise OSError("simulated read error")

        def parse_precise(self, source):  # noqa: ANN001 — stub
            return None

        def scip_index(self, workdir):  # noqa: ANN001 — stub
            return None

    boom = _BoomAdapter()

    def _fake_lookup(ext: str):
        if ext == ".ts":
            return boom
        return None

    monkeypatch.setattr(languages, "lookup_by_extension", _fake_lookup)
    # If pipeline imported lookup_by_extension into its own namespace,
    # patch that too.
    if hasattr(pipeline, "lookup_by_extension"):
        monkeypatch.setattr(
            pipeline, "lookup_by_extension", _fake_lookup, raising=False
        )
    if hasattr(pipeline, "languages"):
        monkeypatch.setattr(
            pipeline.languages,
            "lookup_by_extension",
            _fake_lookup,
            raising=False,
        )

    # Must not raise.
    result = pipeline._analyze_one_file(tmp_path, "broken.ts")
    assert result == [], (
        f"_analyze_one_file must swallow non-python adapter OSError, got {result!r}"
    )
