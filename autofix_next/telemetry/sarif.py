"""SARIF 2.1.0 emitter for autofix_next scan findings.

Emits a deterministic, pretty-printed SARIF 2.1.0 document with one
``result`` per :class:`~autofix_next.evidence.schema.CandidateFinding`.
Every result carries ``partialFingerprints["autofixNext/v1"]`` whose value
is the corresponding ``finding_id`` — this is the stable cross-run
identity used by downstream deduplication (AC #10).

The emitter accepts either a :class:`CandidateFinding` dataclass instance
or a plain ``dict`` with the same logical keys; the latter keeps the
emitter decoupled from the dataclass shape so it can be used from tests
and future consumers that do not yet build full evidence objects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from autofix_next.evidence.schema import CandidateFinding

SARIF_SCHEMA: str = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_VERSION: str = "2.1.0"
DRIVER_NAME: str = "autofix-next"
DRIVER_VERSION: str = "0.1.0"


def _get(finding: Any, *keys: str, default: Any = None) -> Any:
    """Return the first present attribute/key from ``finding``.

    Supports both dataclass-like objects (attribute access) and plain
    mappings (key access). Returns ``default`` if none of ``keys`` is
    populated. This is the single adapter for the mixed-shape input
    contract: :class:`CandidateFinding` vs plain dict with ``relpath``
    rather than ``path``.
    """
    for key in keys:
        if isinstance(finding, Mapping):
            if key in finding and finding[key] is not None:
                return finding[key]
        else:
            val = getattr(finding, key, None)
            if val is not None:
                return val
    return default


def _result_for(finding: Any) -> dict[str, Any]:
    """Build a SARIF 2.1.0 ``result`` object for a single finding.

    The ``partialFingerprints["autofixNext/v1"]`` value is the raw
    ``finding_id`` string so that downstream consumers can join on it
    without further transformation (AC #10).
    """
    finding_id = _get(finding, "finding_id", default="")
    rule_id = _get(finding, "rule_id", default="")
    # Prefer an explicit message; otherwise synthesize one from the symbol.
    message_text = _get(finding, "message")
    symbol_name = _get(finding, "symbol_name", default="")
    if not message_text:
        message_text = (
            f"Unused import: {symbol_name}" if symbol_name else "Finding"
        )
    # ``path`` is the CandidateFinding field name; ``relpath`` is the
    # dict-shape field name used by external callers and tests.
    uri = _get(finding, "path", "relpath", default="")
    start_line = _get(finding, "start_line", default=1)
    end_line = _get(finding, "end_line", default=start_line)
    level = _get(finding, "level", default="warning")

    return {
        "ruleId": rule_id,
        "level": level,
        "message": {"text": message_text},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": uri},
                    "region": {
                        "startLine": start_line,
                        "endLine": end_line,
                    },
                }
            }
        ],
        "partialFingerprints": {"autofixNext/v1": finding_id},
    }


def emit_sarif(
    scan_id: str,
    findings: list[CandidateFinding] | list[dict[str, Any]],
    sarif_path: Path,
) -> Path:
    """Write a SARIF 2.1.0 document to ``sarif_path``.

    Parameters
    ----------
    scan_id:
        Logical identifier for this scan run. Accepted so the emitter
        signature mirrors the future SARIFEmitted event row, but not
        surfaced in the SARIF document itself — the spec pins
        ``invocations[0].arguments`` to an empty list for schema-checker
        compatibility. Downstream correlation with events.jsonl uses the
        ``SARIFEmitted`` row's ``scan_id`` field instead.
    findings:
        Either a list of :class:`CandidateFinding` dataclass instances or
        a list of dicts carrying the same logical keys (``finding_id``,
        ``rule_id``, ``path``/``relpath``, ``start_line``, optional
        ``end_line``, ``symbol_name``, ``message``, ``level``).
    sarif_path:
        Destination path. Its parent directory is created with
        ``parents=True, exist_ok=True``.

    Returns
    -------
    Path
        The resolved ``sarif_path`` actually written.

    Raises
    ------
    OSError
        If the parent directory cannot be created or the file cannot be
        written. Errors from ``json.dumps`` propagate as ``TypeError`` if
        a non-serializable value sneaks into a finding.
    """
    sarif_path = Path(sarif_path)
    sarif_path.parent.mkdir(parents=True, exist_ok=True)

    results = [_result_for(f) for f in findings]

    document: dict[str, Any] = {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": DRIVER_NAME,
                        "version": DRIVER_VERSION,
                    }
                },
                "invocations": [
                    {
                        "executionSuccessful": True,
                        "arguments": [],
                    }
                ],
                "results": results,
            }
        ],
    }

    # ``sort_keys=True`` + ``indent=2`` gives deterministic pretty output:
    # two SARIF files produced from the same findings are byte-identical,
    # which is a precondition for any "did this scan change?" diffing.
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    try:
        sarif_path.write_text(text, encoding="utf-8")
    except OSError:
        raise

    return sarif_path


__all__ = [
    "SARIF_SCHEMA",
    "SARIF_VERSION",
    "DRIVER_NAME",
    "DRIVER_VERSION",
    "emit_sarif",
]
