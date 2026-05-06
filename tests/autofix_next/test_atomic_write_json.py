"""Tests for ``autofix_next.telemetry.atomic.atomic_write_json``
(task-010 AC 14-18).

Covers:
  AC 14 — roundtrip: write succeeds, file is readable JSON with the
         same payload, function returns True.
  AC 15 — deterministic: same payload produces byte-identical output
         regardless of dict insertion order.
  AC 16/18 — cleanup-on-failure: when ``os.replace`` raises OSError,
         no ``<path>.tmp`` leftover survives and the function returns
         False (does NOT raise).
  AC 17 — no chmod side effects: the function must not call
         ``os.chmod`` — file mode is whatever the OS gives a freshly
         ``os.open(O_CREAT, 0o644)`` file after umask. No exec bit is
         ever added.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _helper():
    return pytest.importorskip("autofix_next.telemetry.atomic")


# ---------------------------------------------------------------------------
# AC 14 — roundtrip.
# ---------------------------------------------------------------------------


def test_roundtrip_writes_and_reads_back(tmp_path: Path) -> None:
    """AC 14: ``atomic_write_json`` writes a file we can JSON-decode to
    the original payload and returns True."""
    atomic = _helper()
    target = tmp_path / "out.json"
    payload = {"alpha": 1, "beta": [1, 2, 3], "gamma": {"nested": True}}

    result = atomic.atomic_write_json(target, payload)
    assert result is True
    assert target.exists()
    assert not (tmp_path / "out.json.tmp").exists(), "tmp file must be cleaned"

    # File is valid JSON and decodes to the original mapping.
    decoded = json.loads(target.read_text(encoding="utf-8"))
    assert decoded == payload


def test_roundtrip_with_fsync_dir_false(tmp_path: Path) -> None:
    """AC 14: ``fsync_dir=False`` still produces a correct, readable file.

    This is the hot-loop knob that scip_index uses for per-shard writes;
    skipping the dir fsync must not corrupt the rename.
    """
    atomic = _helper()
    target = tmp_path / "shard.json"
    payload = {"k": "v"}

    assert atomic.atomic_write_json(target, payload, fsync_dir=False) is True
    assert json.loads(target.read_text(encoding="utf-8")) == payload


def test_roundtrip_overwrites_existing_file(tmp_path: Path) -> None:
    """AC 14: atomic_write_json on a pre-existing path overwrites it
    (os.replace semantics). The old bytes are gone after success."""
    atomic = _helper()
    target = tmp_path / "over.json"
    target.write_text('{"old": true}', encoding="utf-8")

    payload = {"new": True, "value": 42}
    assert atomic.atomic_write_json(target, payload) is True
    assert json.loads(target.read_text(encoding="utf-8")) == payload


# ---------------------------------------------------------------------------
# AC 15 — deterministic output.
# ---------------------------------------------------------------------------


def test_same_payload_different_insertion_order_yields_identical_bytes(
    tmp_path: Path,
) -> None:
    """AC 15: serialisation uses ``sort_keys=True, separators=(",", ":")``
    so two dicts with different insertion orders produce byte-identical
    files."""
    atomic = _helper()

    # Two dicts that compare equal but were constructed in different order.
    p1 = {}
    p1["z"] = 1
    p1["a"] = {"k": "v", "m": 2}
    p1["m"] = [3, 1, 2]

    p2 = {}
    p2["a"] = {"m": 2, "k": "v"}
    p2["m"] = [3, 1, 2]
    p2["z"] = 1

    t1 = tmp_path / "one.json"
    t2 = tmp_path / "two.json"

    assert atomic.atomic_write_json(t1, p1) is True
    assert atomic.atomic_write_json(t2, p2) is True

    bytes1 = t1.read_bytes()
    bytes2 = t2.read_bytes()
    assert bytes1 == bytes2, (
        f"deterministic write failed: {bytes1!r} != {bytes2!r}"
    )


def test_no_whitespace_in_serialized_output(tmp_path: Path) -> None:
    """AC 15 (property): deterministic serialisation uses the tight
    ``(",", ":")`` separator — no incidental whitespace leaks into the
    output. Guards against a regression that re-introduces the default
    ``", "``/``": "`` separators (which would still be valid JSON but
    would break byte-identical hashes downstream)."""
    atomic = _helper()
    target = tmp_path / "tight.json"

    assert atomic.atomic_write_json(target, {"a": 1, "b": 2}) is True
    body = target.read_text(encoding="utf-8")
    assert body == '{"a":1,"b":2}', repr(body)


# ---------------------------------------------------------------------------
# AC 16 / AC 18 — failure path cleanup + False return.
# ---------------------------------------------------------------------------


def test_os_replace_failure_returns_false_and_cleans_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 16/18: mocking ``os.replace`` to raise OSError must produce:
    (1) return value False (NOT a raised exception),
    (2) no leftover ``<path>.tmp`` artefact,
    (3) no target file created.
    """
    atomic = _helper()
    target = tmp_path / "doomed.json"
    payload = {"v": 1}

    def _raise_replace(src: str, dst: str) -> None:
        raise OSError("simulated os.replace failure")

    # Patch os.replace both in the stdlib namespace and in the atomic
    # module namespace so whichever reference the helper uses hits the
    # stub.
    monkeypatch.setattr(os, "replace", _raise_replace)

    result = atomic.atomic_write_json(target, payload)

    assert result is False, (
        "atomic_write_json must return False (not raise) on os.replace "
        f"OSError; got {result!r}"
    )
    assert not target.exists(), (
        "target must not exist when os.replace failed"
    )
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], (
        f"no .tmp leftover must remain after os.replace failure; "
        f"found {leftovers!r}"
    )


def test_os_fsync_tmp_failure_still_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 18: an OSError from the tmp-file ``os.fsync`` must also cleanly
    return False and unlink the tmp file. Covers the middle failure
    path (between tmp write and os.replace)."""
    atomic = _helper()
    target = tmp_path / "fsync-fail.json"

    original_fsync = os.fsync
    calls = {"n": 0}

    def _raise_on_file_fsync(fd: int) -> None:
        calls["n"] += 1
        # Raise on the FIRST fsync call (the tmp-fd fsync); if anything
        # else tries to fsync later, let it through so teardown works.
        if calls["n"] == 1:
            raise OSError("simulated tmp fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", _raise_on_file_fsync)

    result = atomic.atomic_write_json(target, {"k": 1})
    assert result is False
    assert not target.exists()
    leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == [], (
        f"no .tmp leftover must remain after fsync failure; "
        f"found {leftovers!r}"
    )


def test_typeerror_propagates_for_non_serializable_payload(
    tmp_path: Path,
) -> None:
    """AC 14/18 (negative): a payload containing a non-JSON-serializable
    value is a programmer bug, not an IO failure — it must propagate as
    TypeError. Silently returning False would hide the defect."""
    atomic = _helper()
    target = tmp_path / "bad.json"

    # set is not JSON-serializable.
    with pytest.raises(TypeError):
        atomic.atomic_write_json(target, {"nope": {1, 2, 3}})

    # Crucially, nothing is written even as a tmp file — serialization
    # happens BEFORE any filesystem work per the docstring contract.
    assert not target.exists()
    assert not (tmp_path / "bad.json.tmp").exists()


# ---------------------------------------------------------------------------
# AC 17 — no chmod side effects.
# ---------------------------------------------------------------------------


def test_no_chmod_called_during_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 17: the helper must NEVER call ``os.chmod``. Callers that need
    a specific mode set it themselves after the write returns True.

    Assertion is implemented by monkeypatching ``os.chmod`` to record
    every invocation — any call fails the test.
    """
    atomic = _helper()
    target = tmp_path / "nochmod.json"

    chmod_calls: list[tuple] = []

    def _recorder(path, mode) -> None:
        chmod_calls.append((path, mode))

    monkeypatch.setattr(os, "chmod", _recorder)

    assert atomic.atomic_write_json(target, {"x": 1}) is True

    assert chmod_calls == [], (
        f"atomic_write_json must not call os.chmod; got {chmod_calls!r}"
    )


def test_no_execute_bit_on_written_file(tmp_path: Path) -> None:
    """AC 17 (property): the resulting file has no execute bit for any
    principal. Output is pure data; no invocation of chmod with an
    executable mode must happen."""
    atomic = _helper()
    target = tmp_path / "exec-bit.json"

    assert atomic.atomic_write_json(target, {"safe": True}) is True

    mode = target.stat().st_mode
    # Mask to the file-mode bits and verify no execute bit for user,
    # group, or other is set.
    assert (mode & 0o111) == 0, (
        f"file must not have any execute bit set; got mode {oct(mode)}"
    )


def test_file_mode_unchanged_by_successive_writes(tmp_path: Path) -> None:
    """AC 17: two successive successful writes do not progressively
    mutate the file mode — guards against a regression that chmods the
    target on each write."""
    atomic = _helper()
    target = tmp_path / "stable-mode.json"

    assert atomic.atomic_write_json(target, {"a": 1}) is True
    mode1 = target.stat().st_mode & 0o777

    assert atomic.atomic_write_json(target, {"b": 2}) is True
    mode2 = target.stat().st_mode & 0o777

    assert mode1 == mode2, (
        f"file mode drift across writes: {oct(mode1)} -> {oct(mode2)}"
    )
    # And no exec bit snuck in between writes either.
    assert (mode2 & 0o111) == 0
