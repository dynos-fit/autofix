"""Tests for ``autofix_next.languages.bin_cache`` (task-006 AC #22..#29,
#39).

Coverage
--------
* AC #22 — module exports ``ensure_binary``, ``BinaryIntegrityError``,
  ``BinaryUnavailableError``.
* AC #23 — unsupported platform raises
  ``BinaryUnavailableError(reason='unsupported_platform')``.
* AC #24 — missing ``_PINNED`` entry raises
  ``BinaryUnavailableError(reason='no_pinned_release')``.
* AC #25 — pre-populated cache with matching SHA256 → fast-path, no
  network.
* AC #25 — pre-populated cache with mismatched SHA256 → ``BinaryIntegrityError``.
* AC #26 — persistent network failure → ``BinaryUnavailableError(reason='network_failure')``.
* AC #27 — downloaded-file checksum mismatch → ``BinaryIntegrityError``
  + ``.tmp`` cleaned up.
* AC #28 — ``AUTOFIX_NEXT_BIN_CACHE`` env var override is honored.
* AC #29 — ``BinaryIntegrityError`` and ``BinaryUnavailableError`` are
  distinct classes (neither subclasses the other).
"""

from __future__ import annotations

import hashlib
import os
import platform
from pathlib import Path
from typing import Any

import pytest


def _import_bin_cache():
    return pytest.importorskip("autofix_next.languages.bin_cache")


# ---------------------------------------------------------------------------
# AC #22 — module exports
# ---------------------------------------------------------------------------


def test_bin_cache_exports_public_surface() -> None:
    """AC #22: ``ensure_binary``, ``BinaryIntegrityError``,
    ``BinaryUnavailableError`` are exported."""
    bc = _import_bin_cache()
    for name in ("ensure_binary", "BinaryIntegrityError", "BinaryUnavailableError"):
        assert hasattr(bc, name), f"bin_cache must expose {name!r}"


# ---------------------------------------------------------------------------
# AC #29 — distinct exception classes
# ---------------------------------------------------------------------------


def test_bin_cache_exceptions_are_distinct_classes() -> None:
    """AC #29: ``BinaryIntegrityError`` and ``BinaryUnavailableError`` are
    neither subclasses of each other.

    This invariant is what lets adapters catch ``BinaryUnavailableError``
    to degrade gracefully while ``BinaryIntegrityError`` propagates to
    abort the scan.
    """
    bc = _import_bin_cache()
    assert not issubclass(bc.BinaryIntegrityError, bc.BinaryUnavailableError), (
        "BinaryIntegrityError must NOT subclass BinaryUnavailableError"
    )
    assert not issubclass(bc.BinaryUnavailableError, bc.BinaryIntegrityError), (
        "BinaryUnavailableError must NOT subclass BinaryIntegrityError"
    )
    # Both must be Exception subclasses.
    assert issubclass(bc.BinaryIntegrityError, Exception)
    assert issubclass(bc.BinaryUnavailableError, Exception)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_pinned_entry_for_current_platform(bc) -> tuple[str, str, str, str]:
    """Return (tool, os_name, arch, version) for an entry present in
    ``_PINNED`` for the current running platform. Skip if none.

    We always use ``scip-typescript`` because it's the first fixture in
    the plan; fall back to ``scip-go`` if only that is pinned.
    """
    pinned = getattr(bc, "_PINNED", None)
    assert pinned is not None, "bin_cache module must expose _PINNED"

    # Resolve current os/arch the way bin_cache will.
    sys_name = platform.system().lower()
    mach = platform.machine().lower()
    # Normalise common aliases.
    arch_aliases = {"aarch64": "arm64", "amd64": "x86_64"}
    arch = arch_aliases.get(mach, mach)
    for tool in ("scip-typescript", "scip-go"):
        key = (tool, sys_name, arch)
        if key in pinned:
            version, _sha = pinned[key]
            return tool, sys_name, arch, version
    pytest.skip(
        f"no _PINNED entry for current platform ({sys_name}, {arch}); skipping"
    )


# ---------------------------------------------------------------------------
# AC #25 — cache-hit fast path (no network)
# ---------------------------------------------------------------------------


def test_ensure_binary_cache_hit_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #25: with a pre-populated cache file whose SHA256 matches the
    pinned checksum, ``ensure_binary`` returns the path immediately and
    does NOT touch the network.

    We enforce the "no network" invariant by monkeypatching any
    ``urlopen`` (via ``urllib.request``) to raise — any attempt to touch
    the network blows up the test.
    """
    bc = _import_bin_cache()
    tool, os_name, arch, version = _get_pinned_entry_for_current_platform(bc)
    pinned_sha = bc._PINNED[(tool, os_name, arch)][1]

    # Set the cache root to tmp_path and pre-populate with bytes whose
    # sha256 equals the pinned value. We achieve that by picking payload
    # bytes whose hash IS pinned_sha — since we can't reverse sha256, we
    # monkeypatch _PINNED to store a sha for arbitrary payload bytes.
    payload = b"fake-binary-payload-for-cache-hit-test"
    computed_sha = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(bc._PINNED, (tool, os_name, arch), (version, computed_sha))

    # Override cache root via env var (AC #28 lets us do this cleanly).
    monkeypatch.setenv("AUTOFIX_NEXT_BIN_CACHE", str(tmp_path))
    target = tmp_path / tool / version / tool
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)

    # Block any network access.
    import urllib.request as urlreq

    def _boom(*a, **kw):
        raise AssertionError("urlopen must NOT be called on cache-hit fast path")

    monkeypatch.setattr(urlreq, "urlopen", _boom, raising=False)
    if hasattr(bc, "urlopen"):
        monkeypatch.setattr(bc, "urlopen", _boom, raising=False)

    result = bc.ensure_binary(tool)
    assert isinstance(result, Path), (
        f"ensure_binary must return a Path, got {type(result).__name__}"
    )
    assert result == target, (
        f"ensure_binary must return the cached path {target!r}, got {result!r}"
    )
    assert result.exists()


# ---------------------------------------------------------------------------
# AC #25 — cache-hit checksum mismatch → BinaryIntegrityError
# ---------------------------------------------------------------------------


def test_ensure_binary_cache_mismatch_raises_integrity_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #25: a cache file whose SHA256 does NOT match the pinned value
    raises ``BinaryIntegrityError`` (the scan must stop).
    """
    bc = _import_bin_cache()
    tool, os_name, arch, version = _get_pinned_entry_for_current_platform(bc)

    # Pin a SHA256 that the payload will not match.
    monkeypatch.setitem(
        bc._PINNED, (tool, os_name, arch), (version, "0" * 64)
    )
    monkeypatch.setenv("AUTOFIX_NEXT_BIN_CACHE", str(tmp_path))

    target = tmp_path / tool / version / tool
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"not-the-pinned-bytes")

    with pytest.raises(bc.BinaryIntegrityError):
        bc.ensure_binary(tool)


# ---------------------------------------------------------------------------
# AC #26 — network failure → BinaryUnavailableError
# ---------------------------------------------------------------------------


def test_ensure_binary_network_failure_raises_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #26: when ``urlopen`` raises ``OSError`` and all retries are
    exhausted, ``ensure_binary`` raises
    ``BinaryUnavailableError(reason='network_failure')``. It must NOT
    raise ``BinaryIntegrityError`` for a network problem.
    """
    bc = _import_bin_cache()
    tool, os_name, arch, version = _get_pinned_entry_for_current_platform(bc)
    monkeypatch.setenv("AUTOFIX_NEXT_BIN_CACHE", str(tmp_path))
    # Empty cache → miss path → network fetch attempted.

    import urllib.request as urlreq

    call_count = {"n": 0}

    def _network_boom(*a, **kw):
        call_count["n"] += 1
        raise OSError("simulated network failure")

    monkeypatch.setattr(urlreq, "urlopen", _network_boom, raising=False)
    if hasattr(bc, "urlopen"):
        monkeypatch.setattr(bc, "urlopen", _network_boom, raising=False)

    # Also patch time.sleep so we don't actually wait for backoff.
    import time as _time

    monkeypatch.setattr(_time, "sleep", lambda *_a, **_kw: None)

    with pytest.raises(bc.BinaryUnavailableError) as excinfo:
        bc.ensure_binary(tool)
    assert getattr(excinfo.value, "reason", None) == "network_failure", (
        f"BinaryUnavailableError.reason must be 'network_failure', "
        f"got {getattr(excinfo.value, 'reason', None)!r}"
    )


# ---------------------------------------------------------------------------
# AC #27 — downloaded-file checksum mismatch → BinaryIntegrityError
# ---------------------------------------------------------------------------


def test_ensure_binary_downloaded_checksum_mismatch_raises_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #27: if the download succeeds but the computed SHA256 does not
    match the pinned value, ``ensure_binary`` raises
    ``BinaryIntegrityError`` and cleans up the tmp file (no partial
    artifact remains).
    """
    bc = _import_bin_cache()
    tool, os_name, arch, version = _get_pinned_entry_for_current_platform(bc)

    # Pin a checksum the download will NOT produce.
    monkeypatch.setitem(bc._PINNED, (tool, os_name, arch), (version, "0" * 64))
    monkeypatch.setenv("AUTOFIX_NEXT_BIN_CACHE", str(tmp_path))

    import urllib.request as urlreq

    class _FakeResp:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self) -> "_FakeResp":
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    def _fake_urlopen(*a, **kw):
        return _FakeResp(b"malicious-or-corrupt-bytes")

    monkeypatch.setattr(urlreq, "urlopen", _fake_urlopen, raising=False)
    if hasattr(bc, "urlopen"):
        monkeypatch.setattr(bc, "urlopen", _fake_urlopen, raising=False)

    with pytest.raises(bc.BinaryIntegrityError):
        bc.ensure_binary(tool)

    # Assert that no *.tmp files remain in the target cache directory
    # after the failure (AC #27 explicitly requires cleanup).
    cache_dir = tmp_path / tool / version
    if cache_dir.exists():
        leftover_tmps = [p for p in cache_dir.iterdir() if p.suffix == ".tmp"]
        assert leftover_tmps == [], (
            f"BinaryIntegrityError path must remove .tmp artifacts, "
            f"found leftover: {leftover_tmps!r}"
        )


# ---------------------------------------------------------------------------
# AC #23 — unsupported platform
# ---------------------------------------------------------------------------


def test_ensure_binary_unsupported_platform_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC #23: ``platform.system() == 'Windows'`` raises
    ``BinaryUnavailableError(reason='unsupported_platform')``.
    """
    bc = _import_bin_cache()

    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    if hasattr(bc, "platform"):
        monkeypatch.setattr(bc.platform, "system", lambda: "Windows", raising=False)
        monkeypatch.setattr(bc.platform, "machine", lambda: "AMD64", raising=False)

    with pytest.raises(bc.BinaryUnavailableError) as excinfo:
        bc.ensure_binary("scip-typescript")
    assert getattr(excinfo.value, "reason", None) == "unsupported_platform", (
        f"BinaryUnavailableError.reason must be 'unsupported_platform', "
        f"got {getattr(excinfo.value, 'reason', None)!r}"
    )


# ---------------------------------------------------------------------------
# AC #24 — no pinned release
# ---------------------------------------------------------------------------


def test_ensure_binary_no_pinned_release_raises_unavailable() -> None:
    """AC #24: a tool name not present in ``_PINNED`` for any platform
    raises ``BinaryUnavailableError(reason='no_pinned_release')``.
    """
    bc = _import_bin_cache()

    with pytest.raises(bc.BinaryUnavailableError) as excinfo:
        bc.ensure_binary("this-tool-has-no-pin-ever-1234567890")
    assert getattr(excinfo.value, "reason", None) == "no_pinned_release", (
        f"BinaryUnavailableError.reason must be 'no_pinned_release', "
        f"got {getattr(excinfo.value, 'reason', None)!r}"
    )


# ---------------------------------------------------------------------------
# AC #28 — AUTOFIX_NEXT_BIN_CACHE env var override
# ---------------------------------------------------------------------------


def test_bin_cache_env_var_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC #28: when ``AUTOFIX_NEXT_BIN_CACHE`` is set, the cache root
    resolves under that path (not ``~/.cache/autofix-next/bin``).

    We verify by pre-populating a binary at
    ``<override>/<tool>/<version>/<tool>``, pinning a checksum for those
    bytes, and calling ``ensure_binary``; the returned path must live
    under the override.
    """
    bc = _import_bin_cache()
    tool, os_name, arch, version = _get_pinned_entry_for_current_platform(bc)

    payload = b"env-var-override-fixture-bytes"
    monkeypatch.setitem(
        bc._PINNED,
        (tool, os_name, arch),
        (version, hashlib.sha256(payload).hexdigest()),
    )

    override_root = tmp_path / "custom-cache"
    override_root.mkdir()
    monkeypatch.setenv("AUTOFIX_NEXT_BIN_CACHE", str(override_root))
    target = override_root / tool / version / tool
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)

    result = bc.ensure_binary(tool)
    assert result == target, (
        f"ensure_binary must respect AUTOFIX_NEXT_BIN_CACHE; "
        f"expected {target!r}, got {result!r}"
    )
    # Sanity: the returned path is under override_root.
    assert str(result).startswith(str(override_root)), (
        f"ensure_binary must return a path under the override root "
        f"{override_root!r}, got {result!r}"
    )
