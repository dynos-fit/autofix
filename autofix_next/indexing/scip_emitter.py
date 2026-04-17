"""Pure-serialization emitter for the scip_json_v1 shard schema.

This module converts a slice of an in-memory
:class:`autofix_next.invalidation.call_graph.CallGraph` for a single
file into a JSON-serializable ``dict`` matching the
``scip_json_v1`` schema pinned in ``design-decisions.md §4`` and the
seg-1 acceptance criteria.

Responsibilities
----------------
* Emit a shard document whose top-level keys are **exactly**
  ``{schema_version, path, content_hash, symbols, occurrences}``
  (AC #4). Any drift surfaces as a schema-mismatch failure in
  :func:`_validate_shard_shape`.
* Each entry in ``symbols`` has **exactly** the 8 keys
  ``{symbol_id, path, name, kind, start_line, end_line, callers,
  callees}`` with ``callers`` / ``callees`` pre-denormalized inline so a
  later ``callers_of`` query against the persisted index is O(1)
  without a cross-shard join (AC #5).
* Each entry in ``occurrences`` has **at least** ``{symbol_id, role,
  start_line}`` with ``role="definition"`` emitted for every declared
  symbol (AC #6).

What this module deliberately does NOT do
-----------------------------------------
* It does **not** invoke an external SCIP upstream CLI (AC #2 / #27).
* It does **not** emit protobuf bytes — the output is a plain Python
  ``dict`` ready for ``json.dumps``.
* It does **not** touch the filesystem. Persistence is
  :mod:`autofix_next.indexing.scip_index`'s responsibility.

The emitter accepts a ``CallGraph`` instance and extracts the symbols
for a single repo-relative path plus that path's inline caller / callee
lists. Sorted-for-determinism everywhere so byte-identical inputs
produce byte-identical shards — required so the content-addressed
fanout under ``shards/<h[0:2]>/<h[2:4]>/<h>.json`` is stable across
builds and the ``git status --porcelain -- .autofix/`` assertion in
AC #3 never sees spurious diffs.
"""

from __future__ import annotations

from typing import Any, Literal

# ----------------------------------------------------------------------
# Schema constants
# ----------------------------------------------------------------------

# Pinned schema version. Bumped only alongside design-decisions.md §4 +
# a migration note in the seg-1 plan.md. Schema drift without a bump is
# a correctness bug.
SCIP_JSON_SCHEMA_VERSION: str = "scip_json_v1"

# The 5 top-level shard keys (AC #4). Frozen set so accidental mutation
# at import time raises.
_SHARD_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {"schema_version", "path", "content_hash", "symbols", "occurrences"}
)

# The 8 SymbolEntry keys (AC #5).
_SYMBOL_ENTRY_KEYS: frozenset[str] = frozenset(
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

# Required keys for every OccurrenceEntry (AC #6). ``role`` may be
# ``"definition"`` (emitted for every declared symbol) or any future
# extension such as ``"reference"``.
_OCCURRENCE_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"symbol_id", "role", "start_line"}
)

# The 3 kinds the schema allows. task-003's ``SymbolInfo.kind`` uses
# ``"function"`` / ``"class"`` only; the shard schema additionally
# accepts ``"method"`` for forward compatibility with a richer emitter.
_ALLOWED_KINDS: frozenset[str] = frozenset({"function", "class", "method"})


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


def emit_document(
    path: str,
    graph: Any,
    content_hash: str,
) -> dict:
    """Build the per-file SCIP shard dict for ``path`` (AC #2 / #4 / #5 / #6).

    Parameters
    ----------
    path:
        Repo-relative POSIX path of the file the shard represents. This
        value lands verbatim in the top-level ``path`` key and in every
        ``SymbolEntry.path``.
    graph:
        The in-memory :class:`CallGraph` instance that knows every
        symbol declared in the repo plus their caller / callee edges.
        We only read, never mutate.
    content_hash:
        The sha256 hex digest of the file's bytes. Stored verbatim in
        the shard's top-level ``content_hash`` key and used downstream
        by :mod:`scip_index` to compute the two-level fanout directory
        path ``shards/<h[0:2]>/<h[2:4]>/<h>.json``.

    Returns
    -------
    dict
        A JSON-serializable shard document with the 5 top-level keys
        pinned by AC #4 and the 8-key ``SymbolEntry`` shape pinned by
        AC #5. Lists inside the document are sorted for deterministic
        output.
    """

    # Collect the symbol ids declared in this file. ``symbols_in`` never
    # raises; it returns an empty frozenset for an unknown path, which
    # results in an empty ``symbols`` / ``occurrences`` list — still a
    # valid shard.
    symbol_ids = sorted(graph.symbols_in(path))

    symbols: list[dict] = []
    occurrences: list[dict] = []

    for sid in symbol_ids:
        try:
            info = graph[sid]
        except KeyError:
            # Defensive: ``symbols_in`` returned an id we can't resolve.
            # Skip it rather than emit a partial entry — the invalidation
            # planner can rebuild on the next pass.
            continue

        # Inline callers / callees (AC #5). Stored as sorted lists so
        # the shard is byte-deterministic. Task-003's CallGraph keeps
        # these as ``dict[str, set[str]]`` where the key is the caller
        # id. We read both dicts defensively — either may be missing
        # the key entirely if the symbol has no edges.
        callers_src = getattr(graph, "_callers", {}) or {}
        callees_src = getattr(graph, "_callees", {}) or {}
        callers_list = sorted(callers_src.get(sid, ()))
        callees_list = sorted(callees_src.get(sid, ()))

        kind = _coerce_kind(info.kind)

        entry = {
            "symbol_id": info.symbol_id,
            "path": info.path,
            "name": info.name,
            "kind": kind,
            "start_line": int(info.start_line),
            "end_line": int(info.end_line),
            "callers": callers_list,
            "callees": callees_list,
        }
        symbols.append(entry)

        # Definition occurrence for every declared symbol (AC #6).
        occurrences.append(
            {
                "symbol_id": info.symbol_id,
                "role": "definition",
                "start_line": int(info.start_line),
            }
        )

    doc: dict = {
        "schema_version": SCIP_JSON_SCHEMA_VERSION,
        "path": path,
        "content_hash": content_hash,
        "symbols": symbols,
        "occurrences": occurrences,
    }

    # Fail closed: any drift in the produced shape is a programming
    # error, not a runtime condition. Validating on emission means the
    # caller never writes a malformed shard to disk.
    _validate_shard_shape(doc)
    return doc


def _coerce_kind(kind: str) -> Literal["function", "class", "method"]:
    """Map a ``SymbolInfo.kind`` onto one of the allowed shard kinds.

    task-003's :class:`SymbolInfo` uses ``"function"`` for both top-level
    functions and class methods. We keep that mapping — the schema's
    ``"method"`` slot stays open for a future emitter that distinguishes
    bound methods explicitly without breaking existing shards.
    """

    if kind in _ALLOWED_KINDS:
        return kind  # type: ignore[return-value]
    # Unknown kind — coerce to "function" so the schema stays happy.
    # In practice task-003 only emits function/class; this branch is a
    # defensive fallback that should never fire in production.
    return "function"


def _validate_shard_shape(doc: dict) -> None:
    """Raise :class:`ValueError` if ``doc`` violates the scip_json_v1 schema.

    Called from :func:`emit_document` before returning so a silent drift
    in the emitter can't smuggle an invalid shard onto disk. Also called
    by :mod:`scip_index` when loading to confirm on-disk shards haven't
    drifted from the pinned schema.

    The check is strict on both directions:

    * Every required top-level key must be present.
    * No extra top-level keys are permitted.
    * Every ``SymbolEntry`` must have exactly the 8 pinned keys.
    * Every ``OccurrenceEntry`` must have at minimum the 3 required
      keys; additional keys are permitted for forward compatibility.
    """

    if not isinstance(doc, dict):
        raise ValueError(
            f"shard must be a dict, got {type(doc).__name__}"
        )

    actual_keys = set(doc.keys())
    if actual_keys != _SHARD_TOP_LEVEL_KEYS:
        missing = _SHARD_TOP_LEVEL_KEYS - actual_keys
        extra = actual_keys - _SHARD_TOP_LEVEL_KEYS
        raise ValueError(
            "shard top-level keys mismatch: "
            f"missing={sorted(missing)} extra={sorted(extra)}"
        )

    if doc["schema_version"] != SCIP_JSON_SCHEMA_VERSION:
        raise ValueError(
            f"shard schema_version must be {SCIP_JSON_SCHEMA_VERSION!r}, "
            f"got {doc['schema_version']!r}"
        )

    if not isinstance(doc["path"], str):
        raise ValueError("shard.path must be a str")
    if not isinstance(doc["content_hash"], str):
        raise ValueError("shard.content_hash must be a str")
    if not isinstance(doc["symbols"], list):
        raise ValueError("shard.symbols must be a list")
    if not isinstance(doc["occurrences"], list):
        raise ValueError("shard.occurrences must be a list")

    for i, entry in enumerate(doc["symbols"]):
        if not isinstance(entry, dict):
            raise ValueError(
                f"shard.symbols[{i}] must be a dict"
            )
        entry_keys = set(entry.keys())
        if entry_keys != _SYMBOL_ENTRY_KEYS:
            missing = _SYMBOL_ENTRY_KEYS - entry_keys
            extra = entry_keys - _SYMBOL_ENTRY_KEYS
            raise ValueError(
                f"shard.symbols[{i}] keys mismatch: "
                f"missing={sorted(missing)} extra={sorted(extra)}"
            )
        if entry["kind"] not in _ALLOWED_KINDS:
            raise ValueError(
                f"shard.symbols[{i}].kind={entry['kind']!r} "
                f"not in {sorted(_ALLOWED_KINDS)}"
            )
        if not isinstance(entry["callers"], list):
            raise ValueError(
                f"shard.symbols[{i}].callers must be a list"
            )
        if not isinstance(entry["callees"], list):
            raise ValueError(
                f"shard.symbols[{i}].callees must be a list"
            )
        if not isinstance(entry["start_line"], int):
            raise ValueError(
                f"shard.symbols[{i}].start_line must be int"
            )
        if not isinstance(entry["end_line"], int):
            raise ValueError(
                f"shard.symbols[{i}].end_line must be int"
            )

    for i, occ in enumerate(doc["occurrences"]):
        if not isinstance(occ, dict):
            raise ValueError(
                f"shard.occurrences[{i}] must be a dict"
            )
        missing = _OCCURRENCE_REQUIRED_KEYS - set(occ.keys())
        if missing:
            raise ValueError(
                f"shard.occurrences[{i}] missing keys {sorted(missing)}"
            )


__all__ = [
    "SCIP_JSON_SCHEMA_VERSION",
    "emit_document",
    "_validate_shard_shape",
]
