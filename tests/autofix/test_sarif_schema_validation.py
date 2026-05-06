"""SARIF 2.1.0 schema-conformance tests for emit_sarif output (ACs 17, 19, 21).

* AC 17: the emitted document validates against the OASIS SARIF 2.1.0
  JSON schema.
* AC 19: validation passes for three scales of input — 0, 1, and 10
  findings — so that empty scans and batched scans both remain valid.
* AC 21: the ``jsonschema`` library is an OPTIONAL test-time dependency.
  When not installed, this module is SKIPPED via
  ``pytest.importorskip`` — it MUST NOT cause the suite to fail.

If the committed SARIF schema fixture is missing, the suite ``fail``s
with a pointer to ``https://json.schemastore.org/sarif-2.1.0.json`` so
the dev can re-populate it rather than guessing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# AC 21: skip (do not fail) when jsonschema is not installed.
jsonschema = pytest.importorskip("jsonschema")

from autofix.telemetry.sarif import emit_sarif


_SCHEMA_FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "sarif-2.1.0.schema.json"
)


@pytest.fixture(scope="module")
def sarif_schema() -> dict:
    """Load the committed OASIS SARIF 2.1.0 schema.

    If the fixture is missing we ``pytest.fail`` rather than ``skip`` —
    a missing fixture is a repo corruption signal, not a soft
    environment issue.
    """
    if not _SCHEMA_FIXTURE.is_file():
        pytest.fail(
            f"SARIF schema fixture missing at {_SCHEMA_FIXTURE}. "
            "Re-download from https://json.schemastore.org/sarif-2.1.0.json "
            "and commit to tests/fixtures/sarif-2.1.0.schema.json.",
            pytrace=False,
        )
    return json.loads(_SCHEMA_FIXTURE.read_text(encoding="utf-8"))


def _fake_finding(i: int) -> dict:
    """A dict-shape finding accepted by emit_sarif."""
    return {
        "finding_id": f"{i:064x}",
        "rule_id": "unused-import.intra-file",
        "message": f"Unused import fixture {i}",
        "relpath": f"pkg/mod_{i}.py",
        "symbol_name": f"sym_{i}",
        "start_line": 1 + i,
        "end_line": 1 + i,
        "level": "warning",
    }


def _emit_and_load(tmp_path: Path, count: int) -> dict:
    findings = [_fake_finding(i) for i in range(count)]
    sarif_path = tmp_path / f"findings_{count}.sarif"
    emit_sarif(
        scan_id="20260417T000000Z-deadbeef",
        findings=findings,
        sarif_path=sarif_path,
    )
    return json.loads(sarif_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("count", [0, 1, 10], ids=["empty", "single", "ten"])
def test_emit_sarif_document_validates_against_schema(
    tmp_path: Path, sarif_schema: dict, count: int
) -> None:
    """AC 17 + AC 19: the document validates at 0, 1, and 10 findings."""
    doc = _emit_and_load(tmp_path, count)
    # Use ``validate`` (not ``is_valid``) so a failure surfaces the
    # offending path in the error message — invaluable for debugging a
    # schema regression introduced downstream.
    jsonschema.validate(instance=doc, schema=sarif_schema)


def test_emit_sarif_result_count_matches_findings(tmp_path: Path) -> None:
    """Paired sanity check: the schema passes AND we still see the
    expected number of results. Otherwise a regression could emit
    zero results but still validate (the schema allows empty
    ``results``)."""
    doc = _emit_and_load(tmp_path, 10)
    results = doc["runs"][0]["results"]
    assert len(results) == 10
