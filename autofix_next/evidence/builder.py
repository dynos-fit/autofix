"""EvidencePacket builder (AC #16).

Given the four identifying fields of a candidate finding plus the rendered
``changed_slice`` and a single analyzer's note, assemble the v1 packet dict
with exactly seven top-level keys and compute its ``prompt_prefix_hash`` via
the canonical serializer.

The emitted object is a lightweight subclass of
:class:`autofix_next.evidence.schema.EvidencePacket` that adds a ``to_json``
method returning the same shape as ``to_dict``. The subclass exists so
test harnesses that prefer a JSON-shaped accessor (``packet.to_json()``)
and runtime code that prefers the typed dict accessor
(``packet.to_dict()``) both see identical bytes.
"""

from __future__ import annotations

from typing import Any

from .fingerprints import compute_prompt_prefix_hash
from .schema import SCHEMA_VERSION, AnalyzerTrace, EvidencePacket

_DEFAULT_ENGINE: str = "unused-import"
_DEFAULT_ENGINE_VERSION: str = "v1"


class _EvidencePacketWithJSON(EvidencePacket):
    """Thin subclass of :class:`EvidencePacket` adding a ``to_json`` alias.

    The base dataclass uses ``slots=True`` which means we cannot simply
    attach a bound function at build time; a subclass is the cleanest way
    to layer one method without bloating the frozen seg-1 schema.
    """

    def to_json(self) -> dict[str, Any]:
        """Return the 7-key JSON-serializable dict (same as ``to_dict``)."""
        return self.to_dict()


def build_packet(
    *,
    rule_id: str,
    relpath: str,
    symbol_name: str,
    normalized_import: str,
    changed_slice: str,
    analyzer_note: str,
    engine: str = _DEFAULT_ENGINE,
    engine_version: str = _DEFAULT_ENGINE_VERSION,
    supporting_symbols: list[str] | None = None,
) -> EvidencePacket:
    """Assemble a frozen-shape :class:`EvidencePacket`.

    Parameters
    ----------
    rule_id:
        The analyzer rule id (e.g. ``"unused-import.intra-file"``).
    relpath:
        Repo-relative POSIX path of the file the finding targets.
    symbol_name:
        The bound name the finding is about (goes into ``primary_symbol``
        after being joined to ``relpath`` by the ``"::"`` separator).
    normalized_import:
        Whitespace-collapsed import text. Stored in the packet only
        indirectly via ``changed_slice``; kept in the signature for
        symmetry with :func:`compute_finding_fingerprint`.
    changed_slice:
        The textual slice the LLM will see as the local context.
    analyzer_note:
        Prose explanation of why the analyzer flagged this symbol; goes
        into the single :class:`AnalyzerTrace` row.
    engine:
        Identifier of the analyzer engine (default ``"unused-import"``).
    engine_version:
        Analyzer version string (default ``"v1"``).
    supporting_symbols:
        Optional list of symbol ids that back the claim. Defaults to an
        empty list per AC #16.

    Returns
    -------
    EvidencePacket
        A fully-populated packet whose ``prompt_prefix_hash`` is computed
        from the other six keys via :func:`compute_prompt_prefix_hash`.
    """
    if not isinstance(rule_id, str) or not rule_id:
        raise ValueError("rule_id must be a non-empty string")
    if not isinstance(relpath, str) or not relpath:
        raise ValueError("relpath must be a non-empty string")
    if not isinstance(symbol_name, str) or not symbol_name:
        raise ValueError("symbol_name must be a non-empty string")
    if not isinstance(changed_slice, str):
        raise ValueError("changed_slice must be a string")
    if not isinstance(analyzer_note, str):
        raise ValueError("analyzer_note must be a string")
    # normalized_import is retained for signature symmetry with the
    # finding-fingerprint helper; we validate but do not embed it.
    if not isinstance(normalized_import, str):
        raise ValueError("normalized_import must be a string")

    primary_symbol = f"{relpath}::{symbol_name}"
    supporting: list[str] = list(supporting_symbols or [])

    # Build the packet-sans-hash dict in canonical key order for
    # readability. ``canonical_json_bytes`` inside
    # ``compute_prompt_prefix_hash`` re-sorts keys so declaration order
    # does not affect the resulting hash.
    packet_dict_without_hash: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "rule_id": rule_id,
        "primary_symbol": primary_symbol,
        "changed_slice": changed_slice,
        "supporting_symbols": supporting,
        "analyzer_traces": [
            {
                "engine": engine,
                "engine_version": engine_version,
                "note": analyzer_note,
            }
        ],
    }

    prompt_prefix_hash = compute_prompt_prefix_hash(packet_dict_without_hash)

    return _EvidencePacketWithJSON(
        schema_version=SCHEMA_VERSION,
        rule_id=rule_id,
        primary_symbol=primary_symbol,
        changed_slice=changed_slice,
        supporting_symbols=supporting,
        analyzer_traces=[
            AnalyzerTrace(
                engine=engine,
                engine_version=engine_version,
                note=analyzer_note,
            )
        ],
        prompt_prefix_hash=prompt_prefix_hash,
    )


__all__ = ["build_packet"]
