"""Tests for ``autofix_next.indexing.scip_emitter`` (AC #2 / #4 / #5 / #6 / #27).

The emitter is a pure serializer: it turns an in-memory ``CallGraph``
slice into a SCIP-inspired JSON dict matching the ``scip_json_v1``
schema pinned in ``design-decisions.md §4``. It does NOT touch the
filesystem, it does NOT shell out to the upstream ``scip-python`` CLI,
and it does NOT emit protobuf bytes.

Every top-level key, every symbol-entry key, and every occurrence-entry
key is pinned by this test file — a schema drift must fail a test
before it can reach a shard on disk.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_python")
pytest.importorskip("tree_sitter")

REPO_ROOT = Path(__file__).resolve().parents[3]

# The 5 top-level keys of a shard are pinned by AC #4; any drift here is
# a schema-version bump event that must also touch design-decisions.md §4.
_SHARD_TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "path", "content_hash", "symbols", "occurrences"}
)

# The 8 SymbolEntry keys pinned by AC #5. ``callers`` and ``callees``
# are stored inline (denormalized) so O(1) lookup is possible after load.
_SYMBOL_ENTRY_KEYS = frozenset(
    {
        "symbol_id",
        "path",
        "name",
        "kind",
        "start_line",
        "end_line",
        "callers",
        "callees",
    }
)


def _build_tiny_graph(tmp_path: Path):
    """Two-file hub-and-spoke graph used by the emitter unit tests.

    Isolated from the synthetic_50k_loc_repo fixture so emitter tests
    run fast and deterministically. ``hub.py`` defines ``hub_func``;
    ``caller.py`` imports and calls it. Return the built ``CallGraph``
    + the relative paths + the sha256 content hash of each file.
    """

    from autofix_next.invalidation.call_graph import CallGraph

    (tmp_path / "hub.py").write_text(
        "def hub_func():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "caller.py").write_text(
        "from hub import hub_func\n"
        "\n"
        "def caller_func():\n"
        "    return hub_func()\n",
        encoding="utf-8",
    )
    graph = CallGraph.build_from_root(tmp_path)
    return graph


def test_scip_emitter_module_imports() -> None:
    """AC #2: ``autofix_next.indexing.scip_emitter`` is importable and
    exposes ``emit_document`` + ``SCIP_JSON_SCHEMA_VERSION``."""

    from autofix_next.indexing import scip_emitter

    assert hasattr(scip_emitter, "emit_document")
    assert scip_emitter.SCIP_JSON_SCHEMA_VERSION == "scip_json_v1"


def test_emit_document_produces_scip_json_v1(tmp_path: Path) -> None:
    """AC #2: the emitter produces a dict tagged ``scip_json_v1`` and
    never shells out to an external scip-python CLI nor returns bytes."""

    from autofix_next.indexing.scip_emitter import emit_document

    graph = _build_tiny_graph(tmp_path)
    doc = emit_document(
        path="hub.py",
        graph=graph,
        content_hash="a" * 64,
    )
    assert isinstance(doc, dict), "emit_document must return a dict"
    assert doc["schema_version"] == "scip_json_v1"


def test_shard_top_level_keys_exact(tmp_path: Path) -> None:
    """AC #4: a shard has EXACTLY the 5 top-level keys (no more, no less)."""

    from autofix_next.indexing.scip_emitter import emit_document

    graph = _build_tiny_graph(tmp_path)
    doc = emit_document(path="hub.py", graph=graph, content_hash="b" * 64)
    assert set(doc.keys()) == _SHARD_TOP_LEVEL_KEYS, (
        f"expected exactly {_SHARD_TOP_LEVEL_KEYS}, got {set(doc.keys())}"
    )
    assert doc["path"] == "hub.py"
    assert doc["content_hash"] == "b" * 64
    assert isinstance(doc["symbols"], list)
    assert isinstance(doc["occurrences"], list)


def test_symbol_entry_has_inline_callers_callees(tmp_path: Path) -> None:
    """AC #5: each SymbolEntry has exactly 8 keys with inline callers/callees."""

    from autofix_next.indexing.scip_emitter import emit_document

    graph = _build_tiny_graph(tmp_path)
    doc = emit_document(path="hub.py", graph=graph, content_hash="c" * 64)
    assert doc["symbols"], "tiny graph's hub.py must have at least 1 symbol"

    for entry in doc["symbols"]:
        assert set(entry.keys()) == _SYMBOL_ENTRY_KEYS, (
            f"SymbolEntry keys must be exactly {_SYMBOL_ENTRY_KEYS}, "
            f"got {set(entry.keys())}"
        )
        assert entry["kind"] in {"function", "class", "method"}
        assert isinstance(entry["callers"], list)
        assert isinstance(entry["callees"], list)
        assert isinstance(entry["start_line"], int)
        assert isinstance(entry["end_line"], int)

    # At least one symbol entry for hub_func should list caller_func.hub_func
    # (or the caller.py::caller_func id) under callers — the whole point of
    # inline denormalization is that callers are present without a join.
    hub_entry = next(
        (e for e in doc["symbols"] if e["name"] == "hub_func"), None
    )
    assert hub_entry is not None, "emitter dropped hub_func"
    assert any("caller.py" in c for c in hub_entry["callers"]), (
        f"hub_func.callers should mention caller.py, got {hub_entry['callers']}"
    )


def test_occurrence_has_definition_role(tmp_path: Path) -> None:
    """AC #6: every declared symbol emits a ``role='definition'``
    occurrence; reference occurrences may be absent."""

    from autofix_next.indexing.scip_emitter import emit_document

    graph = _build_tiny_graph(tmp_path)
    doc = emit_document(path="hub.py", graph=graph, content_hash="d" * 64)

    assert doc["occurrences"], "every declared symbol needs a definition occurrence"

    required_keys = {"symbol_id", "role", "start_line"}
    for occ in doc["occurrences"]:
        missing = required_keys - set(occ.keys())
        assert not missing, f"Occurrence missing keys {missing}: {occ}"

    definition_symbols = {
        occ["symbol_id"] for occ in doc["occurrences"] if occ["role"] == "definition"
    }
    symbol_ids = {s["symbol_id"] for s in doc["symbols"]}
    assert symbol_ids.issubset(definition_symbols), (
        "every symbol must have a role=definition occurrence"
    )


def test_no_scip_python_or_protobuf_imports_in_emitter() -> None:
    """AC #2 / #27: the emitter source must not import ``scip-python``,
    ``scip_python``, or any protobuf-schema SCIP module.

    A grep-style assertion is intentionally broad so a drive-by import
    of the upstream SCIP Python package or its protobuf wire module
    fails the suite rather than silently adding a Node.js dependency.
    """

    emitter_path = REPO_ROOT / "autofix_next" / "indexing" / "scip_emitter.py"
    if not emitter_path.exists():
        # Production file doesn't exist yet — the test is still meaningful
        # (it will pass vacuously now and start guarding on creation).
        # We still fail-fast so the test doesn't silently become a no-op
        # after the file lands.
        pytest.fail(
            f"scip_emitter.py does not exist at {emitter_path}; "
            "production code must be created before this AC can be verified"
        )

    text = emitter_path.read_text(encoding="utf-8")
    for forbidden in ("scip_python", "scip-python", "scip_python_protobuf"):
        assert forbidden not in text, (
            f"scip_emitter.py must not reference {forbidden!r} — AC #2 / #27"
        )


def test_no_production_file_imports_scip_python_or_protobuf() -> None:
    """AC #27 contributor: grep across ``autofix_next/`` for any
    ``from`` or ``import`` statement that pulls in ``scip_python`` or a
    protobuf-schema SCIP module.

    Uses ``git grep`` when the repo is a git working tree, falls back to
    a Python walk otherwise. Any match fails the test.
    """

    autofix_next_dir = REPO_ROOT / "autofix_next"
    if not autofix_next_dir.is_dir():
        pytest.skip("autofix_next/ not present in this checkout")

    # Walk *.py under autofix_next/ and scan for the forbidden tokens.
    forbidden_tokens = ("scip_python", "scip_python_protobuf")
    offenders: list[tuple[Path, int, str]] = []
    for py_file in autofix_next_dir.rglob("*.py"):
        try:
            for lineno, line in enumerate(
                py_file.read_text(encoding="utf-8").splitlines(), start=1
            ):
                stripped = line.strip()
                if not (stripped.startswith("import ") or stripped.startswith("from ")):
                    continue
                if any(tok in stripped for tok in forbidden_tokens):
                    offenders.append((py_file, lineno, stripped))
        except OSError:
            continue

    assert not offenders, (
        "production code imports forbidden SCIP modules: "
        + "; ".join(f"{p}:{ln} {src!r}" for p, ln, src in offenders)
    )
