"""Post-processing of a single LLM invocation into a :class:`ReportRecord`.

The scheduler hands this module the :class:`EvidencePacket` it fed to
the model together with the raw subprocess-style result (returncode,
stdout, stderr). ``write`` parses the model's stdout as best-effort
JSON and returns a flat dataclass suitable for downstream logging and
audit. It is pure: no I/O, no process execution, no network calls.

Untrusted-input note: ``result.stdout`` comes from an external model
and must be treated as arbitrary bytes. We JSON-decode inside a
narrow ``try`` and coerce every unpacked value through ``str(...)``
so a malicious or malformed response cannot smuggle a non-string
``decision``/``reason`` downstream.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from autofix.evidence.schema import EvidencePacket

__all__ = ["ReportRecord", "write"]


class _ResultLike(Protocol):
    """Structural type for the LLM-backend result object.

    Declared locally so this module does not import the concrete
    backend at runtime (keeping the grep-based isolation test green).
    The attribute set matches the backend's frozen dataclass.
    """

    returncode: int
    stdout: str
    stderr: str


@dataclass(slots=True)
class ReportRecord:
    """Flat post-processing record for a single LLM invocation."""

    decision: str
    reason: str
    rule_id: str
    primary_symbol: str
    prompt_prefix_hash: str
    model_stdout: str


def write(packet: EvidencePacket, result: _ResultLike) -> ReportRecord:
    """Build a :class:`ReportRecord` from ``packet`` and ``result``.

    ``result.stdout`` is parsed as JSON best-effort. On any decode
    failure, ``decision`` is set to ``"unknown"`` and ``reason`` to
    ``"stdout not JSON"``. On success, ``decision`` and ``reason`` are
    extracted from the top-level dict; missing keys default to the
    empty string. Non-``dict`` JSON payloads (e.g. a bare list or
    string) are treated as a decode failure.
    """
    stdout = result.stdout

    decision: str
    reason: str
    try:
        parsed = json.loads(stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        decision = "unknown"
        reason = "stdout not JSON"
    else:
        if isinstance(parsed, dict):
            decision = str(parsed.get("decision", "")) if parsed.get("decision") is not None else ""
            reason = str(parsed.get("reason", "")) if parsed.get("reason") is not None else ""
        else:
            decision = "unknown"
            reason = "stdout not JSON"

    return ReportRecord(
        decision=decision,
        reason=reason,
        rule_id=packet.rule_id,
        primary_symbol=packet.primary_symbol,
        prompt_prefix_hash=packet.prompt_prefix_hash,
        model_stdout=stdout,
    )
