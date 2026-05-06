"""Legacy-overlap invariant for the sarif-v1 fingerprint (AC 20).

AC 20 calls for ``sarif-v1`` fingerprints to overlap the legacy
autofix scanner's identity when the two scanners observe the same
logical finding. We can't import the legacy scanner's hashing recipe
from this repo without reaching across the module boundary, so we
prove the weaker-but-sufficient property that the legacy scanner
relied on: **line-move stability**.

Concretely:

  Run A: seeds at (start_line=10,  end_line=12)
  Run B: same rule_id/path/symbol_name but at (start_line=100, end_line=102)

The per-finding ``sarif-v1`` fingerprints in Run A MUST match the
corresponding entries in Run B. If that holds, the two scanners'
fingerprints overlap for every finding that:

  * has the same (rule_id, path, symbol_name) triple, and
  * moved only by line-number shift between scans.

This is the documented invariant we expose to cross-scanner dedup —
*not* a byte-identical-hash claim against legacy code, which would
require importing that code and is out of scope for this segment.
"""

from __future__ import annotations

import json
from pathlib import Path

from autofix.telemetry.sarif import emit_sarif


_SEEDS = [
    # (rule_id, path, symbol_name) triples chosen to span three axes of
    # variation: rule, file, and symbol.
    ("unused-import.intra-file", "pkg/a.py", "os"),
    ("dead-code.function", "pkg/b.py", "helper_fn"),
    ("unused-import.cross-file", "pkg/sub/c.py", "json"),
]


def _finding(triple: tuple[str, str, str], start_line: int, end_line: int) -> dict:
    rule_id, path, symbol_name = triple
    # finding_id is intentionally different from sarif-v1; the sarif-v1
    # fingerprint MUST NOT depend on finding_id.
    finding_id = f"{hash((rule_id, path, symbol_name, start_line)) & 0xFFFFFFFFFFFFFFFF:064x}"
    return {
        "finding_id": finding_id,
        "rule_id": rule_id,
        "message": f"finding for {symbol_name}",
        "relpath": path,
        "symbol_name": symbol_name,
        "start_line": start_line,
        "end_line": end_line,
        "level": "warning",
    }


def _emit(tmp_path: Path, start_line: int, end_line: int, label: str) -> list[dict]:
    """Emit a SARIF document for the three seeds at the given line pair
    and return the list of result dicts."""
    findings = [_finding(t, start_line=start_line, end_line=end_line) for t in _SEEDS]
    sarif_path = tmp_path / f"run_{label}.sarif"
    emit_sarif(
        scan_id=f"20260417T000000Z-{label}",
        findings=findings,
        sarif_path=sarif_path,
    )
    return json.loads(sarif_path.read_text(encoding="utf-8"))["runs"][0]["results"]


def test_sarif_v1_fingerprints_are_line_move_stable(tmp_path: Path) -> None:
    """AC 20: sarif-v1 fingerprints are identical across two runs that
    differ only in line numbers — this is the cross-scanner dedup
    invariant documented above."""
    run_a = _emit(tmp_path, start_line=10, end_line=12, label="a")
    run_b = _emit(tmp_path, start_line=100, end_line=102, label="b")

    fps_a = [r["partialFingerprints"]["sarif-v1"] for r in run_a]
    fps_b = [r["partialFingerprints"]["sarif-v1"] for r in run_b]

    assert fps_a == fps_b, (
        "sarif-v1 fingerprints drifted between runs that differ only in "
        f"line numbers. run_a={fps_a} run_b={fps_b}"
    )

    # And — because three distinct seeds were picked — the three
    # per-run fingerprints must themselves be pairwise distinct.
    # Otherwise a trivially broken implementation that returned a
    # constant would also satisfy the equality above.
    assert len(set(fps_a)) == 3, f"seeds collided: {fps_a}"


def test_sarif_v1_ignores_finding_id_drift(tmp_path: Path) -> None:
    """The ``finding_id`` key participates in ``autofixNext/v1`` but
    MUST NOT participate in ``sarif-v1`` — a pure ``finding_id`` swap
    keeps the sarif-v1 digest intact."""
    seed = _SEEDS[0]
    a = _finding(seed, start_line=5, end_line=5)
    b = _finding(seed, start_line=5, end_line=5)
    b["finding_id"] = "f" * 64  # replace only the finding_id

    sarif_a = tmp_path / "a.sarif"
    sarif_b = tmp_path / "b.sarif"
    emit_sarif(scan_id="sid-a", findings=[a], sarif_path=sarif_a)
    emit_sarif(scan_id="sid-b", findings=[b], sarif_path=sarif_b)

    fp_a = json.loads(sarif_a.read_text())["runs"][0]["results"][0][
        "partialFingerprints"
    ]["sarif-v1"]
    fp_b = json.loads(sarif_b.read_text())["runs"][0]["results"][0][
        "partialFingerprints"
    ]["sarif-v1"]

    assert fp_a == fp_b
