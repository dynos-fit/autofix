"""EvidencePacket v1 dataclasses (AC #16).

The packet has exactly 7 top-level keys:

    schema_version, rule_id, primary_symbol, changed_slice,
    supporting_symbols, analyzer_traces, prompt_prefix_hash

``analyzer_traces`` is a list of length 1 whose sole element carries the
keys ``engine``, ``engine_version``, ``note``. Any additional key on the
packet or on an analyzer trace is a contract violation; tests at
``tests/autofix_next/test_evidence_builder.py`` enforce this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION: str = "evidence_v1"


@dataclass(slots=True)
class AnalyzerTrace:
    """A single analyzer's attribution record.

    Exactly three keys. Adding a field here is a schema change and must
    bump ``SCHEMA_VERSION`` in lockstep.
    """

    engine: str
    engine_version: str
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "engine_version": self.engine_version,
            "note": self.note,
        }


@dataclass(slots=True)
class EvidencePacket:
    """The frozen v1 evidence shape (AC #16).

    Field declaration order matches the documented packet-key order so that
    ``to_dict`` emits keys in the canonical order. The downstream
    ``canonical_json_bytes`` serializer re-sorts keys alphabetically, so
    declaration order is documentation rather than a hash input — but
    preserving it avoids surprises in human-readable diagnostic output.
    """

    schema_version: str
    rule_id: str
    primary_symbol: str
    changed_slice: str
    supporting_symbols: list[str]
    analyzer_traces: list[AnalyzerTrace]
    prompt_prefix_hash: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dict with the 7 canonical keys.

        Nested ``analyzer_traces`` entries are unwrapped to plain dicts so
        the result is safe to pass to ``canonical_json_bytes`` without
        further conversion.
        """
        return {
            "schema_version": self.schema_version,
            "rule_id": self.rule_id,
            "primary_symbol": self.primary_symbol,
            "changed_slice": self.changed_slice,
            "supporting_symbols": list(self.supporting_symbols),
            "analyzer_traces": [t.to_dict() for t in self.analyzer_traces],
            "prompt_prefix_hash": self.prompt_prefix_hash,
        }


@dataclass(slots=True)
class CandidateFinding:
    """A candidate finding produced by an analyzer, pre-LLM.

    ``finding_id`` is the stable fingerprint computed via
    ``autofix_next.evidence.fingerprints.compute_finding_fingerprint``.
    The seg-2 builder is responsible for populating it; this dataclass is
    intentionally a plain record.
    """

    rule_id: str
    path: str
    symbol_name: str
    normalized_import: str
    start_line: int
    end_line: int
    changed_slice: str
    finding_id: str
    analyzer_confidence: float = field(default=1.0, kw_only=True)


__all__ = [
    "SCHEMA_VERSION",
    "AnalyzerTrace",
    "EvidencePacket",
    "CandidateFinding",
]
