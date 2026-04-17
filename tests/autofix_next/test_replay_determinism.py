"""Tests for replay determinism and locked-path byte-identity.

Covers:
  AC #9  — two scans of the same clean working tree produce byte-identical
           finding_id and prompt_prefix_hash values for every finding.
  AC #19 — no file under any of the seven locked paths is modified by a scan,
           with the explicit exception that .autofix/events.jsonl grows
           append-only (pre-existing byte prefix preserved).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Locked path globs from spec AC #19. `.autofix/events.jsonl` is an
# exception — it grows append-only during a scan; only its pre-existing
# prefix must be byte-identical.
LOCKED_GLOBS_STRICT: list[str] = [
    # Fully locked (no append-only carve-out):
    "autofix/llm_io/**",
    "autofix/agent_loop.py",
    "autofix/llm_backend.py",
    ".autofix/autofix-policy.json",
    "benchmarks/agent_bench/**",
]
LOCKED_GLOBS_STATE: list[str] = [
    ".autofix/state/**",
]
APPEND_ONLY_FILE = ".autofix/events.jsonl"


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=root, check=True)


def _commit(root: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", message],
        cwd=root,
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-04-17T00:00:00Z",
            "GIT_COMMITTER_DATE": "2026-04-17T00:00:00Z",
        },
    )


@pytest.fixture
def scan_repo(tmp_path: Path) -> Path:
    """Two-commit repo seeded with an unused import. Also pre-populates the
    locked paths with known content so we can assert byte-identity after a
    scan."""
    _init_git_repo(tmp_path)
    (tmp_path / "module_a.py").write_text(
        "import os\n\npath = os.getcwd()\n", encoding="utf-8"
    )
    _commit(tmp_path, "initial")
    (tmp_path / "module_b.py").write_text(
        "import json  # unused on purpose\n\nx = 1\n", encoding="utf-8"
    )
    _commit(tmp_path, "add unused import")

    # Seed locked paths so we can verify byte-identity.
    autofix_dir = tmp_path / "autofix"
    autofix_dir.mkdir(parents=True, exist_ok=True)
    (autofix_dir / "llm_backend.py").write_text("# locked\n", encoding="utf-8")
    (autofix_dir / "agent_loop.py").write_text("# locked\n", encoding="utf-8")
    (autofix_dir / "llm_io").mkdir(parents=True, exist_ok=True)
    (autofix_dir / "llm_io" / "x.py").write_text("# locked\n", encoding="utf-8")

    hidden = tmp_path / ".autofix"
    hidden.mkdir(parents=True, exist_ok=True)
    (hidden / "autofix-policy.json").write_text(
        json.dumps({"suppressions": []}), encoding="utf-8"
    )
    (hidden / "state").mkdir(parents=True, exist_ok=True)
    (hidden / "state" / "current.json").write_text("{}", encoding="utf-8")
    # Pre-existing events.jsonl prefix (append-only exception).
    (hidden / "events.jsonl").write_text(
        '{"event":"legacy","at":"2026-04-01T00:00:00Z"}\n', encoding="utf-8"
    )

    bench = tmp_path / "benchmarks" / "agent_bench"
    bench.mkdir(parents=True, exist_ok=True)
    (bench / "fixture.txt").write_text("locked\n", encoding="utf-8")

    return tmp_path


def _collect_locked_snapshot(root: Path) -> dict[str, bytes]:
    """Read every file under the strict locked globs + the state glob
    into a {relpath: sha256-bytes} map. Returns empty dict if nothing matched."""
    snapshot: dict[str, bytes] = {}
    for pattern in LOCKED_GLOBS_STRICT + LOCKED_GLOBS_STATE:
        for path in root.glob(pattern):
            if path.is_file():
                rel = str(path.relative_to(root))
                snapshot[rel] = hashlib.sha256(path.read_bytes()).hexdigest().encode()
    return snapshot


def _run_scan_in_subprocess(root: Path) -> int:
    """Run autofix-next scan in a subprocess so side effects can't leak into
    the test process. run_prompt is faked via a small bootstrap script."""
    bootstrap = (
        "import sys\n"
        "from autofix.llm_backend import LLMResult\n"
        "def _fake(prompt, **kw):\n"
        "    return LLMResult(returncode=0, stdout='{\"decision\":\"confirmed\"}', stderr='')\n"
        "import autofix.llm_backend as _mod\n"
        "_mod.run_prompt = _fake\n"
        f"sys.argv = ['autofix-next', 'scan', '--root', {str(root)!r}]\n"
        "from autofix_next.cli.main import main\n"
        f"raise SystemExit(main(['scan', '--root', {str(root)!r}]))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", bootstrap],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    return completed.returncode


def _collect_findings_from_scan(root: Path) -> list[dict]:
    scans_dir = root / ".autofix-next" / "scans"
    if not scans_dir.is_dir():
        return []
    # Pick the latest scan directory (sorted by name = ISO-8601 sortable).
    subdirs = sorted(scans_dir.iterdir())
    assert subdirs, f"no scan dirs under {scans_dir}"
    sarif_path = subdirs[-1] / "findings.sarif"
    assert sarif_path.is_file(), f"SARIF missing: {sarif_path}"
    doc = json.loads(sarif_path.read_text(encoding="utf-8"))
    return doc["runs"][0]["results"]


def test_finding_ids_byte_identical_across_runs(scan_repo: Path) -> None:
    """AC #9 (finding_id): two scans of the same tree yield byte-identical
    finding_id values for every emitted finding."""
    rc_a = _run_scan_in_subprocess(scan_repo)
    assert rc_a == 0, "first scan must exit zero"
    findings_a = _collect_findings_from_scan(scan_repo)
    assert findings_a, "first scan produced no findings"
    ids_a = sorted(r["partialFingerprints"]["autofixNext/v1"] for r in findings_a)

    # Clear the output dir between runs so the second run regenerates it.
    shutil.rmtree(scan_repo / ".autofix-next", ignore_errors=True)

    rc_b = _run_scan_in_subprocess(scan_repo)
    assert rc_b == 0, "second scan must exit zero"
    findings_b = _collect_findings_from_scan(scan_repo)
    ids_b = sorted(r["partialFingerprints"]["autofixNext/v1"] for r in findings_b)

    assert ids_a == ids_b, (
        f"finding_id set differed across runs:\n  a={ids_a!r}\n  b={ids_b!r}"
    )


def test_prompt_prefix_hashes_byte_identical_across_runs(scan_repo: Path) -> None:
    """AC #9 (prompt_prefix_hash): two scans emit byte-identical
    EvidencePacketBuilt.prompt_prefix_hash values.  Read from events.jsonl."""
    events_path = scan_repo / ".autofix" / "events.jsonl"

    pre_len = len(events_path.read_bytes())

    rc_a = _run_scan_in_subprocess(scan_repo)
    assert rc_a == 0
    new_a = events_path.read_bytes()[pre_len:]
    hashes_a = sorted(
        row.get("scan_event", {}).get("prompt_prefix_hash")
        for line in new_a.splitlines()
        if line.strip()
        for row in [json.loads(line)]
        if row.get("event") == "EvidencePacketBuilt"
    )
    assert hashes_a, "first scan did not log any EvidencePacketBuilt hashes"

    # Clear outputs between runs; events.jsonl stays append-only so we track
    # the new suffix for the second run.
    shutil.rmtree(scan_repo / ".autofix-next", ignore_errors=True)
    mid_len = len(events_path.read_bytes())

    rc_b = _run_scan_in_subprocess(scan_repo)
    assert rc_b == 0
    new_b = events_path.read_bytes()[mid_len:]
    hashes_b = sorted(
        row.get("scan_event", {}).get("prompt_prefix_hash")
        for line in new_b.splitlines()
        if line.strip()
        for row in [json.loads(line)]
        if row.get("event") == "EvidencePacketBuilt"
    )

    assert hashes_a == hashes_b, (
        f"prompt_prefix_hash set differed across runs:\n  a={hashes_a!r}\n  b={hashes_b!r}"
    )


def test_locked_paths_byte_identical_after_scan(scan_repo: Path) -> None:
    """AC #19: a scan does not modify any file under the locked paths
    (autofix/llm_io/**, autofix/agent_loop.py, autofix/llm_backend.py,
    .autofix/state/**, .autofix/autofix-policy.json, benchmarks/agent_bench/**).
    .autofix/events.jsonl is the documented exception: its pre-existing byte
    prefix must remain byte-identical; only appended bytes are allowed."""
    pre_strict = _collect_locked_snapshot(scan_repo)
    pre_events_bytes = (scan_repo / APPEND_ONLY_FILE).read_bytes()

    rc = _run_scan_in_subprocess(scan_repo)
    assert rc == 0, "scan must exit zero"

    post_strict = _collect_locked_snapshot(scan_repo)
    assert post_strict == pre_strict, (
        f"locked file hashes diverged:\n  pre={pre_strict!r}\n  post={post_strict!r}"
    )

    post_events_bytes = (scan_repo / APPEND_ONLY_FILE).read_bytes()
    assert post_events_bytes.startswith(pre_events_bytes), (
        "events.jsonl pre-existing prefix must remain byte-identical (append-only)"
    )
