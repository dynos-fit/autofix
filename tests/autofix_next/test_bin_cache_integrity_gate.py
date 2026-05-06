"""Tests for the bin-cache supply-chain integrity gate (task-010 AC 1-4).

Covers:
  AC 1 — ``_is_placeholder_sha(value)`` behaves on placeholder strings,
         real 64-char lowercase hex digests, and the empty string.
  AC 2 — ``ensure_binary`` raises ``BinCacheIntegrityError`` with the
         exact spec-mandated message format when the pinned SHA256 for
         the requested tool is a sentinel placeholder.
  AC 3 — ``BinCacheIntegrityError`` is a ``RuntimeError`` subclass,
         distinct from ``BinaryIntegrityError``.
  AC 4 — The gate fires BEFORE cache directory creation / network work,
         so a placeholder-pinned tool does not leave partial artefacts.
"""

from __future__ import annotations

import hashlib
import platform
from pathlib import Path

import pytest


def _bc():
    return pytest.importorskip("autofix_next.languages.bin_cache")


def _current_platform_key(bc):
    """Return a ``(tool, os_name, arch, version)`` tuple for an entry
    present in ``_PINNED`` for the current running platform, or skip.

    Mirrors the helper in ``tests/autofix_next/languages/test_bin_cache.py``
    so this test file is self-contained.
    """
    pinned = getattr(bc, "_PINNED", None)
    assert pinned is not None, "bin_cache must expose _PINNED"
    sys_name = platform.system().lower()
    mach = platform.machine().lower()
    arch_aliases = {"aarch64": "arm64", "amd64": "x86_64"}
    arch = arch_aliases.get(mach, mach)
    for tool in ("scip-typescript", "scip-go"):
        key = (tool, sys_name, arch)
        if key in pinned:
            version, _sha = pinned[key]
            return tool, sys_name, arch, version
    pytest.skip(
        f"no _PINNED entry for current platform ({sys_name}, {arch})"
    )


# ---------------------------------------------------------------------------
# AC 1 — _is_placeholder_sha predicate behaviour.
# ---------------------------------------------------------------------------


def test_is_placeholder_sha_matches_sentinel_prefix_and_suffix() -> None:
    """AC 1: a string that starts with ``<sha256-placeholder`` and ends
    with ``>`` is recognised as a placeholder."""
    bc = _bc()

    assert bc._is_placeholder_sha("<sha256-placeholder-ts-darwin-arm64>") is True
    assert bc._is_placeholder_sha("<sha256-placeholder>") is True
    # Real production entries use a dashed suffix — verify it still matches.
    assert bc._is_placeholder_sha("<sha256-placeholder-go-linux-x86_64>") is True


def test_is_placeholder_sha_rejects_real_hex_digest() -> None:
    """AC 1: a real 64-character lowercase hex SHA256 is NOT a placeholder.

    Uses a freshly computed digest from deterministic input so the test
    is reproducible.
    """
    bc = _bc()

    real = hashlib.sha256(b"scip-go-binary-bytes").hexdigest()
    assert len(real) == 64, "sha256 hexdigest is always 64 chars"
    assert real.islower(), "sha256 hexdigest is lowercase"
    assert bc._is_placeholder_sha(real) is False

    # Also verify a straightforwardly-constructed 64-hex string.
    synthetic = "a" * 64
    assert bc._is_placeholder_sha(synthetic) is False

    # And a 64-char string that happens to start with '<' but not the
    # sentinel prefix.
    weird = "<" + "b" * 63
    assert bc._is_placeholder_sha(weird) is False


def test_is_placeholder_sha_rejects_empty_and_partial_sentinels() -> None:
    """AC 1: the empty string and partial-sentinel shapes are NOT
    placeholders. Guards against a regression where the predicate
    accidentally returns True for any input with a leading ``<``."""
    bc = _bc()

    assert bc._is_placeholder_sha("") is False
    # Prefix without closing >:
    assert bc._is_placeholder_sha("<sha256-placeholder-unterminated") is False
    # Trailing > without prefix:
    assert bc._is_placeholder_sha("not-a-placeholder>") is False
    # Wrong prefix:
    assert bc._is_placeholder_sha("<sha512-placeholder-wrong>") is False


# ---------------------------------------------------------------------------
# AC 3 — BinCacheIntegrityError is a distinct class.
# ---------------------------------------------------------------------------


def test_bin_cache_integrity_error_is_runtime_error_subclass() -> None:
    """AC 3: ``BinCacheIntegrityError`` subclasses ``RuntimeError`` and is
    distinct from ``BinaryIntegrityError`` (the latter guards runtime
    byte mismatches; the former is a configuration defect)."""
    bc = _bc()

    assert issubclass(bc.BinCacheIntegrityError, RuntimeError)
    assert not issubclass(bc.BinCacheIntegrityError, bc.BinaryIntegrityError)
    assert not issubclass(bc.BinaryIntegrityError, bc.BinCacheIntegrityError)
    # Not silently subclassing BinaryUnavailableError either — callers
    # catching "unavailable" must NOT accidentally swallow the integrity
    # gate.
    assert not issubclass(bc.BinCacheIntegrityError, bc.BinaryUnavailableError)


# ---------------------------------------------------------------------------
# AC 2 — ensure_binary raises BinCacheIntegrityError on placeholder.
# ---------------------------------------------------------------------------


def test_ensure_binary_raises_integrity_error_for_placeholder_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 2: ``ensure_binary`` raises ``BinCacheIntegrityError`` with the
    spec-mandated message when ``_PINNED[(tool, os, arch)]`` is a
    sentinel placeholder. The message must name the offending tool and
    instruct the operator to set a real 64-char lowercase hex sha256."""
    bc = _bc()
    tool, os_name, arch, version = _current_platform_key(bc)

    # Force a placeholder pin for the current platform. We do NOT rely on
    # the module's baseline placeholders because a future task may swap
    # them for real digests; instead, we explicitly install a sentinel.
    sentinel = "<sha256-placeholder-test-integrity-gate>"
    monkeypatch.setitem(bc._PINNED, (tool, os_name, arch), (version, sentinel))
    monkeypatch.setenv("AUTOFIX_NEXT_BIN_CACHE", str(tmp_path))

    with pytest.raises(bc.BinCacheIntegrityError) as excinfo:
        bc.ensure_binary(tool)

    # Exact message shape from AC #2:
    #   f"pinned sha256 for {tool!r} is a placeholder; "
    #   f"set a real 64-char lowercase hex sha256 in _PINNED"
    msg = str(excinfo.value)
    assert "pinned sha256 for" in msg, msg
    # ``repr(tool)`` wraps the name in quotes; assert the raw name and
    # the quoted form both appear so future formatter-string refactors
    # do not silently drop either invariant.
    assert tool in msg, msg
    assert repr(tool) in msg, msg
    assert "placeholder" in msg, msg
    assert "64-char lowercase hex" in msg, msg
    assert "_PINNED" in msg, msg


def test_ensure_binary_placeholder_pin_does_not_touch_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 4: the integrity gate fires BEFORE the cache-miss network path
    executes. Monkeypatch ``urllib.request.urlopen`` to blow up on any
    call; the test must still raise ``BinCacheIntegrityError``, proving
    the gate short-circuits before any HTTP attempt."""
    bc = _bc()
    tool, os_name, arch, version = _current_platform_key(bc)

    sentinel = "<sha256-placeholder-test-no-network>"
    monkeypatch.setitem(bc._PINNED, (tool, os_name, arch), (version, sentinel))
    monkeypatch.setenv("AUTOFIX_NEXT_BIN_CACHE", str(tmp_path))

    import urllib.request as urlreq

    def _boom(*a, **kw):
        raise AssertionError(
            "urlopen must NOT be called when the pinned sha is a placeholder"
        )

    monkeypatch.setattr(urlreq, "urlopen", _boom, raising=False)
    if hasattr(bc, "urlopen"):
        monkeypatch.setattr(bc, "urlopen", _boom, raising=False)

    with pytest.raises(bc.BinCacheIntegrityError):
        bc.ensure_binary(tool)


def test_ensure_binary_placeholder_pin_does_not_create_cache_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 4: the integrity gate fires BEFORE ``cache_dir.mkdir(...)`` —
    a placeholder-pinned tool must not leave ``<cache_root>/<tool>/<version>/``
    behind as an artefact, because doing so would mask the config defect
    with filesystem state.
    """
    bc = _bc()
    tool, os_name, arch, version = _current_platform_key(bc)

    sentinel = "<sha256-placeholder-test-no-mkdir>"
    monkeypatch.setitem(bc._PINNED, (tool, os_name, arch), (version, sentinel))
    monkeypatch.setenv("AUTOFIX_NEXT_BIN_CACHE", str(tmp_path))

    with pytest.raises(bc.BinCacheIntegrityError):
        bc.ensure_binary(tool)

    # The cache root may or may not exist — but the per-tool dir MUST
    # not. Otherwise a silent ``mkdir`` would indicate the gate fired
    # AFTER fs work.
    tool_dir = tmp_path / tool / version
    assert not tool_dir.exists(), (
        f"placeholder pin must not create {tool_dir}; gate fired too late"
    )


def test_ensure_binary_placeholder_gate_fires_before_no_pinned_release() -> None:
    """AC 2 + AC 4: the gate is ordered AFTER the ``_PINNED`` lookup (so
    a missing pin still yields ``BinaryUnavailableError(reason="no_pinned_release")``)
    but BEFORE any other work. A tool that is wholly absent from
    ``_PINNED`` must NOT trigger the integrity gate — the missing-pin
    path owns that case."""
    bc = _bc()

    with pytest.raises(bc.BinaryUnavailableError) as excinfo:
        bc.ensure_binary("this-tool-is-not-pinned-anywhere-xyz123")
    assert getattr(excinfo.value, "reason", None) == "no_pinned_release"
    # And specifically: this must not be a BinCacheIntegrityError.
    assert not isinstance(excinfo.value, bc.BinCacheIntegrityError)
