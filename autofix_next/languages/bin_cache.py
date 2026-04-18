"""Download-on-first-use cache for scip-typescript / scip-go external binaries.

Pinned ``(version, SHA256)`` table keyed by ``(tool, os, arch)``. Cache root
honors ``AUTOFIX_NEXT_BIN_CACHE`` or defaults to ``~/.cache/autofix-next/bin/``
(AC #28).

Exception contract is asymmetric (AC #29):

* :class:`BinaryIntegrityError` — SHA256 mismatch on cache OR downloaded
  file. ALWAYS aborts the scan; adapters MUST NOT catch this.
* :class:`BinaryUnavailableError` — unsupported platform / no pinned
  release / network failure. Adapters catch this and degrade to
  cheap-path-only.

The two classes are DISTINCT: neither subclasses the other. This lets
callers write ``except BinaryUnavailableError`` without accidentally
swallowing integrity violations.

The atomic-install sequence (fsync tmp → fsync parent → ``os.replace`` →
fsync parent) mirrors :meth:`autofix_next.indexing.scip_index.SCIPIndex._atomic_write_json`
verbatim (AC #27). Per-cache-directory flock mirrors ``_acquire_lock`` in
the same module.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import importlib
import os
import time
import urllib.error
import urllib.request

# ``platform`` is imported via importlib to hide it from the legacy
# AST-based import-graph builder (autofix.platform.build_import_graph),
# which stem-matches ``import platform`` against the repo-local
# ``autofix/platform.py`` and produces a false-positive edge. Mirrors
# the same workaround adopted by autofix_next/languages/python.py.
platform = importlib.import_module("platform")
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class BinaryIntegrityError(Exception):
    """Raised on SHA256 mismatch — cached file or fresh download.

    Adapters MUST NOT catch this: an integrity violation aborts the scan
    so a tampered / corrupt binary can never be executed. Distinct from
    :class:`BinaryUnavailableError` (AC #29).
    """


class BinaryUnavailableError(Exception):
    """Raised on unsupported platform / no pinned release / network failure.

    Adapters catch this to degrade to cheap-path-only scanning. Distinct
    from :class:`BinaryIntegrityError` (AC #29).

    Attributes
    ----------
    reason:
        One of ``"unsupported_platform"``, ``"no_pinned_release"``,
        ``"network_failure"``.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# Pinned (version, sha256) table keyed by (tool, os, arch).
#
# NOTE: the SHA256 values below are PLACEHOLDERS. Resolving real values
# requires fetching each GitHub Release's checksum manifest at build time;
# the implementation executor cannot hit the network, so the follow-up
# commit MUST replace every ``"<sha256-placeholder-*>"`` with the actual
# 64-hex-char digest from upstream SHA256SUMS. Tests monkeypatch this
# dict, so they do not depend on the placeholder values being real.
_PINNED: dict[tuple[str, str, str], tuple[str, str]] = {
    ("scip-typescript", "darwin", "arm64"): ("0.3.30", "<sha256-placeholder-ts-darwin-arm64>"),
    ("scip-typescript", "darwin", "x86_64"): ("0.3.30", "<sha256-placeholder-ts-darwin-x86_64>"),
    ("scip-typescript", "linux", "x86_64"): ("0.3.30", "<sha256-placeholder-ts-linux-x86_64>"),
    ("scip-go", "darwin", "arm64"): ("0.1.17", "<sha256-placeholder-go-darwin-arm64>"),
    ("scip-go", "darwin", "x86_64"): ("0.1.17", "<sha256-placeholder-go-darwin-x86_64>"),
    ("scip-go", "linux", "x86_64"): ("0.1.17", "<sha256-placeholder-go-linux-x86_64>"),
}

# Supported platform tuples (AC #23).
_SUPPORTED_PLATFORMS: frozenset[tuple[str, str]] = frozenset(
    {
        ("darwin", "arm64"),
        ("darwin", "x86_64"),
        ("linux", "x86_64"),
    }
)

_DOWNLOAD_TIMEOUT_SEC: float = 60.0
_DOWNLOAD_RETRIES: int = 2  # total attempts = _DOWNLOAD_RETRIES + 1

# Mirror SCIPIndex's flock constants verbatim.
LOCK_TIMEOUT_SECONDS: float = 30.0
LOCK_INITIAL_BACKOFF: float = 0.05
LOCK_MAX_BACKOFF: float = 1.0


def _resolve_platform() -> tuple[str, str]:
    """Return ``(os_name, arch)`` with common aliases normalized.

    ``platform.machine()`` surfaces ``aarch64`` on Linux ARM and
    ``arm64`` on macOS ARM; ``amd64`` on some BSDs and ``x86_64`` on
    GNU/Linux. Normalize so :data:`_PINNED` only has to list canonical
    keys.
    """
    os_name = platform.system().lower()
    arch = platform.machine().lower()
    if arch in {"aarch64", "arm64"}:
        arch = "arm64"
    elif arch in {"x86_64", "amd64"}:
        arch = "x86_64"
    return os_name, arch


def _cache_root() -> Path:
    """Return cache root; honors ``AUTOFIX_NEXT_BIN_CACHE`` (AC #28)."""
    env = os.environ.get("AUTOFIX_NEXT_BIN_CACHE")
    if env:
        return Path(env).expanduser()
    return Path("~/.cache/autofix-next/bin").expanduser()


def _sha256_of_file(path: Path) -> str:
    """Compute SHA256 of ``path`` by streaming 64 KiB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_url(tool: str, version: str, os_name: str, arch: str) -> str:
    """Return the upstream GitHub Releases URL for ``(tool, version, os, arch)``.

    The URL templates match Sourcegraph's standard release-asset naming.
    If real releases use a different pattern (``.tar.gz`` archive, etc.),
    the follow-up commit that resolves real SHA256 values must update
    this template in the same edit.
    """
    if tool == "scip-typescript":
        return (
            f"https://github.com/sourcegraph/scip-typescript/releases/download/"
            f"v{version}/scip-typescript-{os_name}-{arch}"
        )
    if tool == "scip-go":
        return (
            f"https://github.com/sourcegraph/scip-go/releases/download/"
            f"v{version}/scip-go-{os_name}-{arch}"
        )
    # Defensive: unreachable because ``_PINNED`` keys gate the call, but
    # we keep the guard so a future new pinned tool surfaces a clear
    # error instead of silently producing an invalid URL.
    raise BinaryUnavailableError(
        f"no known download URL template for tool={tool!r}",
        reason="no_pinned_release",
    )


@contextmanager
def _acquire_lock(lock_path: Path) -> Iterator[int]:
    """Flock + retry-with-backoff.

    Mirrors :meth:`autofix_next.indexing.scip_index.SCIPIndex._acquire_lock`
    (duplication is accepted per the seg-3 plan — the two modules would
    otherwise need a new shared dependency for a 30-line helper).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        backoff = LOCK_INITIAL_BACKOFF
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(min(backoff, max(0.0, deadline - time.monotonic())))
                backoff = min(backoff * 2, LOCK_MAX_BACKOFF)
            except OSError as exc:
                if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                    if time.monotonic() >= deadline:
                        raise BlockingIOError(
                            f"flock timeout after {LOCK_TIMEOUT_SECONDS}s"
                        ) from exc
                    time.sleep(
                        min(backoff, max(0.0, deadline - time.monotonic()))
                    )
                    backoff = min(backoff * 2, LOCK_MAX_BACKOFF)
                    continue
                raise
        try:
            yield fd
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def _atomic_install(tmp: Path, final: Path) -> None:
    """4-step atomic-rename mirroring ``SCIPIndex._atomic_write_json``.

    Steps:

    1. ``chmod +x`` the tmp file (AC #27).
    2. ``fsync`` the tmp fd so bytes are durable.
    3. ``fsync`` the parent dir so the tmp entry is durable.
    4. ``os.replace(tmp, final)`` — atomic wrt readers.
    5. ``fsync`` the parent dir again so the renamed entry is durable.
    """
    # Step 1: mark executable.
    os.chmod(tmp, 0o755)

    # Step 2: fsync tmp file contents.
    fd = os.open(str(tmp), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

    # Steps 3–5: fsync parent, atomic rename, fsync parent again.
    parent_fd = os.open(str(tmp.parent), os.O_RDONLY)
    try:
        try:
            os.fsync(parent_fd)
        except OSError:
            # Some filesystems (tmpfs, certain test envs) reject fsync
            # on directory descriptors; the rename step below is still
            # atomic wrt readers, so downgrade to best-effort.
            pass
        os.replace(str(tmp), str(final))
        try:
            os.fsync(parent_fd)
        except OSError:
            pass
    finally:
        os.close(parent_fd)


def ensure_binary(tool: str) -> Path:
    """Return the local filesystem path to a verified ``tool`` binary.

    Resolution order:

    1. Resolve current ``(os, arch)``; unsupported platform →
       :class:`BinaryUnavailableError` (AC #23).
    2. Look up ``(tool, os, arch)`` in :data:`_PINNED`; missing →
       :class:`BinaryUnavailableError` with ``reason="no_pinned_release"``
       (AC #24).
    3. Cache-hit fast path: if
       ``<cache_root>/<tool>/<version>/<tool>`` exists and its SHA256
       matches, return it (AC #25). Mismatch →
       :class:`BinaryIntegrityError`.
    4. Cache-miss: acquire per-cache-dir flock, download with
       60-s timeout and 2 retries (AC #26), verify SHA256 (AC #27),
       atomic install.

    Raises
    ------
    BinaryUnavailableError
        Recoverable: platform not supported, no pin, or persistent
        network failure.
    BinaryIntegrityError
        Non-recoverable: cached or downloaded file failed checksum.
    """
    # AC #23: platform gate.
    os_name, arch = _resolve_platform()
    if (os_name, arch) not in _SUPPORTED_PLATFORMS:
        raise BinaryUnavailableError(
            f"unsupported platform: os={os_name!r} arch={arch!r}",
            reason="unsupported_platform",
        )

    # AC #24: pin lookup.
    pin_key = (tool, os_name, arch)
    if pin_key not in _PINNED:
        raise BinaryUnavailableError(
            f"no pinned release for tool={tool!r} os={os_name!r} arch={arch!r}",
            reason="no_pinned_release",
        )
    version, expected_sha = _PINNED[pin_key]

    cache_dir = _cache_root() / tool / version
    final = cache_dir / tool
    lock_path = cache_dir / ".lock"

    # AC #25: cache-hit fast path — no network, no lock.
    if final.exists():
        actual = _sha256_of_file(final)
        if actual == expected_sha:
            return final
        raise BinaryIntegrityError(
            f"cached {tool!r} checksum mismatch at {final}: "
            f"expected {expected_sha}, got {actual}"
        )

    # AC #26: cache miss — prepare dir, acquire flock, download, verify.
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BinaryUnavailableError(
            f"unable to create cache dir {cache_dir}: {exc}",
            reason="network_failure",
        ) from exc

    tmp = cache_dir / f"{tool}.tmp"
    url = _download_url(tool, version, os_name, arch)

    with _acquire_lock(lock_path):
        # Another process may have finished the download while we waited
        # for the lock; re-check before firing the request.
        if final.exists():
            actual = _sha256_of_file(final)
            if actual == expected_sha:
                return final
            raise BinaryIntegrityError(
                f"cached {tool!r} checksum mismatch at {final} after lock: "
                f"expected {expected_sha}, got {actual}"
            )

        # Retry loop: 1 initial attempt + _DOWNLOAD_RETRIES retries.
        last_exc: Exception | None = None
        for attempt in range(_DOWNLOAD_RETRIES + 1):
            try:
                with urllib.request.urlopen(
                    url, timeout=_DOWNLOAD_TIMEOUT_SEC
                ) as resp:
                    body = resp.read()
                with open(tmp, "wb") as out:
                    out.write(body)
                break
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last_exc = exc
                # Best-effort cleanup of partial tmp.
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
                if attempt < _DOWNLOAD_RETRIES:
                    # Exponential backoff: 0.5s, 1.0s.
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise BinaryUnavailableError(
                    f"download of {tool!r} from {url} failed after "
                    f"{_DOWNLOAD_RETRIES + 1} attempts: {exc}",
                    reason="network_failure",
                ) from exc
        else:  # pragma: no cover - loop always ends via break or raise
            # Defensive: unreachable — the loop either breaks on success
            # or raises BinaryUnavailableError on the final attempt.
            raise BinaryUnavailableError(
                f"download of {tool!r} from {url} exhausted retries: {last_exc}",
                reason="network_failure",
            )

        # AC #27: verify downloaded SHA256 before install.
        try:
            actual = _sha256_of_file(tmp)
        except OSError as exc:
            # Can't even read the tmp file we just wrote — treat as
            # network/IO failure rather than integrity (we have no
            # evidence the bytes are wrong, only that we can't read them).
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            raise BinaryUnavailableError(
                f"unable to hash downloaded {tool!r}: {exc}",
                reason="network_failure",
            ) from exc

        if actual != expected_sha:
            # Remove the tmp file so a retry gets a clean slate and no
            # partial artifact lingers (AC #27).
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            raise BinaryIntegrityError(
                f"downloaded {tool!r} checksum mismatch from {url}: "
                f"expected {expected_sha}, got {actual}"
            )

        # Integrity proven — install atomically.
        _atomic_install(tmp, final)
        return final


__all__ = [
    "BinaryIntegrityError",
    "BinaryUnavailableError",
    "ensure_binary",
]
