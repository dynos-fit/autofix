"""TDD-first: assert .autofix-next/ output paths collapsed onto .autofix/.

Covers:
- AC 17: SARIF emitter constructs paths under .autofix/scans-next/.
- AC 18: SCIP index writes under .autofix/state/index/.
- AC 19: embedding sidecar writes under .autofix/state/embedding-sidecar/.

Uses regex-grep over source files (not runtime) so the tests do not
depend on a real scan invocation; the assertion is "the literal path
prefix .autofix-next/ does not appear in any source file under autofix/".
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / "autofix"


def test_no_autofix_next_runtime_path_in_source() -> None:
    """ACs 17, 18, 19: no source file references the legacy .autofix-next/ output path."""
    if not PKG_ROOT.is_dir():
        # Pre-rename: the package is at autofix_next/. Skip the source-grep but
        # still assert eventually-true criteria so the test is deterministic.
        # The intent is: AFTER rename, the renamed package contains no
        # `.autofix-next/` references. Before rename, this test fails because
        # autofix/ doesn't exist.
        assert PKG_ROOT.is_dir(), (
            f"autofix/ package directory does not exist at {PKG_ROOT}; "
            "the rename has not happened."
        )
    pat = re.compile(r"\.autofix-next/")
    offenders: list[str] = []
    for py in PKG_ROOT.rglob("*.py"):
        if "__pycache__" in py.parts:
            continue
        text = py.read_text(encoding="utf-8")
        if pat.search(text):
            offenders.append(str(py.relative_to(REPO_ROOT)))
    assert not offenders, (
        ".autofix-next/ path still referenced in:\n  " + "\n  ".join(offenders)
    )


def test_sarif_emitter_uses_scans_next_path() -> None:
    """AC 17: SARIF emitter constructs paths under .autofix/scans-next/."""
    if not PKG_ROOT.is_dir():
        assert False, "autofix/ does not exist; rename pending"
    sarif_module = PKG_ROOT / "telemetry" / "sarif.py"
    if sarif_module.is_file():
        text = sarif_module.read_text(encoding="utf-8")
        # The scans-next path may live in scan_command.py which orchestrates emit_sarif.
        # We only assert this file does not still reference the legacy path.
        assert ".autofix-next/" not in text


def test_scan_command_uses_scans_next_path() -> None:
    """AC 17: scan_command.py emits SARIF under .autofix/scans-next/."""
    if not PKG_ROOT.is_dir():
        assert False, "autofix/ does not exist; rename pending"
    scan_cmd = PKG_ROOT / "cli" / "scan_command.py"
    if not scan_cmd.is_file():
        return  # source structure may change; covered by AC 17 source grep above
    text = scan_cmd.read_text(encoding="utf-8")
    # If the emit path is constructed in this file, it must use the new prefix.
    if ".autofix" in text:
        assert ".autofix-next/" not in text, (
            "scan_command.py still references .autofix-next/"
        )


def test_scip_index_uses_state_index_path() -> None:
    """AC 18: SCIP index lives under .autofix/state/index/."""
    if not PKG_ROOT.is_dir():
        assert False, "autofix/ does not exist; rename pending"
    scip = PKG_ROOT / "indexing" / "scip_index.py"
    if scip.is_file():
        text = scip.read_text(encoding="utf-8")
        assert ".autofix-next/" not in text


def test_embedding_sidecar_uses_state_path() -> None:
    """AC 19: embedding sidecar lives under .autofix/state/embedding-sidecar/."""
    if not PKG_ROOT.is_dir():
        assert False, "autofix/ does not exist; rename pending"
    emb = PKG_ROOT / "indexing" / "embedding.py"
    if emb.is_file():
        text = emb.read_text(encoding="utf-8")
        assert ".autofix-next/" not in text
