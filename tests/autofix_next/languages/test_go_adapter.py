"""Tests for ``autofix_next.languages.go.GoAdapter`` (task-006 AC #15..#21,
#40).

Coverage
--------
* AC #15 — language == 'go', extensions == ('.go',), ``available: bool``.
* AC #16 / #19 / #40 — per-module grouping: subprocess invocation count
  equals distinct module-root count for file lists spanning 1, 2, and 3
  distinct ancestor ``go.mod`` roots.
* AC #17 / #20 — cache reuse: back-to-back invocations with identical
  inputs call subprocess once on the first run and zero times on the
  second.
* AC #18 — nested modules: ``pkg/util/go.mod`` under ``cmd/go.mod``
  routes files to the innermost ``go.mod``.
* Files with no ancestor ``go.mod`` are dropped without invoking
  subprocess for them (risk-note #5).
* AC #17 — cache key formula exactly equals
  ``sha256(module_path + "|" + sha256(go.mod) + "|" + sha256(go.sum or b""))``.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

import pytest


def _import_go():
    return pytest.importorskip("autofix_next.languages.go")


# ---------------------------------------------------------------------------
# AC #15 — extensions + language
# ---------------------------------------------------------------------------


def test_go_adapter_extensions_and_language() -> None:
    """AC #15: language == 'go', extensions == ('.go',), has ``available``."""
    go_mod = _import_go()
    GoAdapter = go_mod.GoAdapter
    adapter = GoAdapter()
    assert adapter.language == "go", (
        f"GoAdapter.language must be 'go', got {adapter.language!r}"
    )
    assert adapter.extensions == (".go",), (
        f"GoAdapter.extensions must be ('.go',), got {adapter.extensions!r}"
    )
    assert hasattr(adapter, "available"), (
        "GoAdapter must expose ``available: bool``"
    )
    assert isinstance(adapter.available, bool)


# ---------------------------------------------------------------------------
# Helpers for subprocess monkeypatching
# ---------------------------------------------------------------------------


def _make_fake_run(tmp_path: Path, fake_bin: Path):
    """Build a ``subprocess.run``-compatible fake that records each call
    and writes an empty ``.scip`` file to the requested ``--output`` path
    so the adapter's persist step can proceed.
    """
    calls: list[list[str]] = []

    def fake_run(cmd, *args: Any, **kwargs: Any):
        calls.append(list(cmd))
        # Find --output <path> in the cmd and touch a fake .scip file.
        try:
            out_idx = cmd.index("--output")
            out_path = Path(cmd[out_idx + 1])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"FAKE_SCIP_BYTES")
        except (ValueError, IndexError):
            pass
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"", stderr=b"")

    return fake_run, calls


def _make_go_tree(root: Path, module_spec: dict[str, list[str]]) -> None:
    """Build a minimal Go module tree.

    ``module_spec`` maps ``<relative_module_path> -> [<file_rel_path>, ...]``.
    A ``go.mod`` is created at every module_path; each listed file is
    created empty (a trivial ``package x\\n`` body).
    """
    for module_rel, files in module_spec.items():
        mod_dir = root / module_rel if module_rel else root
        mod_dir.mkdir(parents=True, exist_ok=True)
        (mod_dir / "go.mod").write_text(
            f"module example.com/{module_rel or 'root'}\n\ngo 1.21\n",
            encoding="utf-8",
        )
        for file_rel in files:
            f = root / file_rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("package x\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# AC #16 / #19 / #40 — per-module grouping produces N subprocess calls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        # One module, multiple files → 1 subprocess call.
        {"": ["a.go", "b.go"]},
        # Two independent modules → 2 subprocess calls.
        {"mod1": ["mod1/x.go"], "mod2": ["mod2/y.go"]},
        # Three independent modules → 3 subprocess calls.
        {
            "mod1": ["mod1/x.go"],
            "mod2": ["mod2/y.go"],
            "mod3": ["mod3/z.go"],
        },
    ],
    ids=["one-module", "two-modules", "three-modules"],
)
def test_go_adapter_groups_by_nearest_go_mod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spec: dict[str, list[str]]
) -> None:
    """AC #16 / #19 / #40: subprocess invocation count equals the distinct
    module-root count in the input file list.
    """
    go_mod = _import_go()
    GoAdapter = go_mod.GoAdapter

    _make_go_tree(tmp_path, spec)

    # Stub bin_cache to avoid a real binary download.
    from autofix_next.languages import bin_cache

    fake_bin = tmp_path / "scip-go-fake"
    fake_bin.write_bytes(b"fake")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(bin_cache, "ensure_binary", lambda tool: fake_bin)

    fake_run, calls = _make_fake_run(tmp_path, fake_bin)
    monkeypatch.setattr(subprocess, "run", fake_run)
    # Some implementations may import subprocess into their module
    # namespace (``from subprocess import run``); patch both.
    if hasattr(go_mod, "subprocess"):
        monkeypatch.setattr(go_mod.subprocess, "run", fake_run, raising=False)
    if hasattr(go_mod, "run"):
        monkeypatch.setattr(go_mod, "run", fake_run, raising=False)

    adapter = GoAdapter()
    # Provide the changed files as a flat list of absolute paths.
    changed_files = [
        tmp_path / rel for files in spec.values() for rel in files
    ]

    adapter.scip_index(tmp_path, changed_files=changed_files)

    expected = len(spec)
    assert len(calls) == expected, (
        f"expected {expected} subprocess.run call(s) (one per distinct "
        f"module root), got {len(calls)}: {calls!r}"
    )


# ---------------------------------------------------------------------------
# AC #18 — nested modules get their own shards
# ---------------------------------------------------------------------------


def test_go_adapter_nested_modules_get_own_shards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #18: nested ``pkg/util/go.mod`` under ``cmd/go.mod`` routes
    files in ``pkg/util/`` to the inner module, not the repo-root module.

    We reuse the shipped fixture tree ``fixtures/mixed_repo/go/`` which
    has exactly this structure:

        fixtures/mixed_repo/go/go.mod                   <-- outer
        fixtures/mixed_repo/go/cmd/app/main.go          (uses outer)
        fixtures/mixed_repo/go/pkg/util/go.mod          <-- inner
        fixtures/mixed_repo/go/pkg/util/util.go         (uses inner)
    """
    go_mod = _import_go()
    GoAdapter = go_mod.GoAdapter

    fixtures_root = (
        Path(__file__).resolve().parent / "fixtures" / "mixed_repo" / "go"
    )
    assert (fixtures_root / "go.mod").is_file(), (
        "outer go.mod fixture missing"
    )
    assert (fixtures_root / "pkg" / "util" / "go.mod").is_file(), (
        "inner go.mod fixture missing"
    )

    from autofix_next.languages import bin_cache

    fake_bin = tmp_path / "scip-go-fake"
    fake_bin.write_bytes(b"fake")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(bin_cache, "ensure_binary", lambda tool: fake_bin)

    fake_run, calls = _make_fake_run(tmp_path, fake_bin)
    monkeypatch.setattr(subprocess, "run", fake_run)
    if hasattr(go_mod, "subprocess"):
        monkeypatch.setattr(go_mod.subprocess, "run", fake_run, raising=False)
    if hasattr(go_mod, "run"):
        monkeypatch.setattr(go_mod, "run", fake_run, raising=False)

    adapter = GoAdapter()
    changed = [
        fixtures_root / "cmd" / "app" / "main.go",  # outer module
        fixtures_root / "pkg" / "util" / "util.go",  # inner module
    ]
    adapter.scip_index(fixtures_root, changed_files=changed)

    # Two distinct module roots must produce two subprocess invocations
    # AND the --module arg of each must point at its own go.mod dir.
    assert len(calls) == 2, (
        f"expected 2 subprocess invocations (one per nested module), "
        f"got {len(calls)}: {calls!r}"
    )
    module_args: list[str] = []
    for cmd in calls:
        if "--module" in cmd:
            idx = cmd.index("--module")
            module_args.append(cmd[idx + 1])
    module_args_resolved = {str(Path(p).resolve()) for p in module_args}
    expected = {
        str(fixtures_root.resolve()),
        str((fixtures_root / "pkg" / "util").resolve()),
    }
    assert module_args_resolved == expected, (
        f"expected --module args {expected!r}, got {module_args_resolved!r}"
    )


# ---------------------------------------------------------------------------
# AC #17 / #20 — cache-reuse: back-to-back invocations
# ---------------------------------------------------------------------------


def test_go_adapter_cache_reuse_skips_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #17 / #20: running the adapter twice over the same fixture with
    identical state calls ``subprocess.run`` on the first pass and skips
    it entirely on the second pass (the shard file is found on disk and
    a ``LanguageShardPersisted(cache_mode="reused")`` row is emitted
    instead).
    """
    go_mod = _import_go()
    GoAdapter = go_mod.GoAdapter

    _make_go_tree(tmp_path, {"": ["a.go"]})

    from autofix_next.languages import bin_cache

    fake_bin = tmp_path / "scip-go-fake"
    fake_bin.write_bytes(b"fake")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(bin_cache, "ensure_binary", lambda tool: fake_bin)

    fake_run, calls = _make_fake_run(tmp_path, fake_bin)
    monkeypatch.setattr(subprocess, "run", fake_run)
    if hasattr(go_mod, "subprocess"):
        monkeypatch.setattr(go_mod.subprocess, "run", fake_run, raising=False)
    if hasattr(go_mod, "run"):
        monkeypatch.setattr(go_mod, "run", fake_run, raising=False)

    adapter = GoAdapter()
    changed = [tmp_path / "a.go"]

    # First invocation: cache miss → one subprocess call + persist.
    adapter.scip_index(tmp_path, changed_files=changed)
    first_count = len(calls)
    assert first_count == 1, (
        f"first invocation must call subprocess once, got {first_count}"
    )

    # Second invocation: cache hit → zero new subprocess calls.
    adapter.scip_index(tmp_path, changed_files=changed)
    second_count = len(calls)
    assert second_count == 1, (
        f"second invocation must be cache-hit with zero new subprocess "
        f"calls (total stays at 1), got {second_count}"
    )


# ---------------------------------------------------------------------------
# risk-note #5 — files without ancestor go.mod are dropped
# ---------------------------------------------------------------------------


def test_go_adapter_drops_files_without_ancestor_go_mod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``.go`` path with no ancestor ``go.mod`` is silently dropped —
    ``subprocess.run`` is not called for that file's module (which does
    not exist) and the adapter does not raise.
    """
    go_mod = _import_go()
    GoAdapter = go_mod.GoAdapter

    # Build a path under tmp_path with NO go.mod anywhere.
    (tmp_path / "orphan").mkdir()
    orphan_file = tmp_path / "orphan" / "x.go"
    orphan_file.write_text("package x\n", encoding="utf-8")

    from autofix_next.languages import bin_cache

    fake_bin = tmp_path / "scip-go-fake"
    fake_bin.write_bytes(b"fake")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(bin_cache, "ensure_binary", lambda tool: fake_bin)

    fake_run, calls = _make_fake_run(tmp_path, fake_bin)
    monkeypatch.setattr(subprocess, "run", fake_run)
    if hasattr(go_mod, "subprocess"):
        monkeypatch.setattr(go_mod.subprocess, "run", fake_run, raising=False)
    if hasattr(go_mod, "run"):
        monkeypatch.setattr(go_mod, "run", fake_run, raising=False)

    adapter = GoAdapter()
    adapter.scip_index(tmp_path, changed_files=[orphan_file])

    assert len(calls) == 0, (
        f"orphaned .go file (no ancestor go.mod) must not trigger a "
        f"subprocess call, got {len(calls)}: {calls!r}"
    )


# ---------------------------------------------------------------------------
# AC #17 — cache-key formula
# ---------------------------------------------------------------------------


def test_go_adapter_cache_key_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #17: the adapter's cache key formula MUST be exactly
    ``sha256(module_path + "|" + sha256(go.mod) + "|" + sha256(go.sum or b""))``.

    We drive the adapter to persist a shard and verify the resulting
    shard filename matches the formula for the fixture's module+go.mod
    bytes.
    """
    go_mod = _import_go()
    GoAdapter = go_mod.GoAdapter

    _make_go_tree(tmp_path, {"": ["a.go"]})

    from autofix_next.languages import bin_cache

    fake_bin = tmp_path / "scip-go-fake"
    fake_bin.write_bytes(b"fake")
    fake_bin.chmod(0o755)
    monkeypatch.setattr(bin_cache, "ensure_binary", lambda tool: fake_bin)

    fake_run, _ = _make_fake_run(tmp_path, fake_bin)
    monkeypatch.setattr(subprocess, "run", fake_run)
    if hasattr(go_mod, "subprocess"):
        monkeypatch.setattr(go_mod.subprocess, "run", fake_run, raising=False)
    if hasattr(go_mod, "run"):
        monkeypatch.setattr(go_mod, "run", fake_run, raising=False)

    adapter = GoAdapter()
    changed = [tmp_path / "a.go"]
    adapter.scip_index(tmp_path, changed_files=changed)

    # Compute expected cache key: the spec pins the exact formula.
    module_path = str(tmp_path)
    gomod_bytes = (tmp_path / "go.mod").read_bytes()
    gosum_bytes = b""  # no go.sum in this fixture
    expected = hashlib.sha256(
        module_path.encode("utf-8")
        + b"|"
        + hashlib.sha256(gomod_bytes).hexdigest().encode("utf-8")
        + b"|"
        + hashlib.sha256(gosum_bytes).hexdigest().encode("utf-8")
    ).hexdigest()

    shard_dir = tmp_path / ".autofix-next" / "state" / "index" / "scip-go"
    assert shard_dir.is_dir(), (
        "scip-go shard directory must be created on first persist"
    )
    shards = list(shard_dir.glob("*.scip"))
    assert len(shards) == 1, (
        f"exactly one shard must be persisted on cold run, got {shards!r}"
    )
    actual_key = shards[0].stem
    assert actual_key == expected, (
        f"shard cache key must be "
        f"sha256(module_path + '|' + sha256(go.mod) + '|' + sha256(go.sum or b'')); "
        f"expected {expected!r}, got {actual_key!r}"
    )


def test_go_adapter_parse_precise_returns_none() -> None:
    """AC #15 (derived): ``parse_precise`` returns ``None`` today —
    precision arrives via ``scip_index``, not ``parse_precise``."""
    go_mod = _import_go()
    GoAdapter = go_mod.GoAdapter
    adapter = GoAdapter()
    assert adapter.parse_precise(b"package x\n") is None


def test_go_adapter_registered_by_default() -> None:
    """AC #5: ``GoAdapter`` is registered at import time and ``.go`` maps
    to it."""
    pytest.importorskip("autofix_next.languages.go")
    from autofix_next import languages

    adapter = languages.lookup_by_extension(".go")
    assert adapter is not None
    assert adapter.language == "go"
