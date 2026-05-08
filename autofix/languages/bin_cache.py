# ruff: noqa: E402  (`importlib.import_module("platform")` is used
# instead of `import platform` to hide the import from the legacy
# AST-based import-graph builder — see the inline comment.)
"""Download-on-first-use cache for scip-go (the only auto-downloaded
external binary today).

Pinned ``(version, SHA256)`` table keyed by ``(tool, os, arch)``. Cache root
honors ``AUTOFIX_BIN_CACHE`` or defaults to ``~/.cache/autofix/bin/``
(AC #28).

scip-typescript ships exclusively via npm
(``@sourcegraph/scip-typescript``) and is not auto-downloaded by this
module — there are no upstream binary release assets to fetch.
:func:`ensure_binary("scip-typescript")` raises
:class:`BinaryUnavailableError` with ``reason="no_pinned_release"``;
the JSTSAdapter degrades to a non-precision path.

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
fsync parent) mirrors :meth:`autofix.indexing.scip_index.SCIPIndex._atomic_write_json`
verbatim (AC #27). Per-cache-directory flock mirrors ``_acquire_lock`` in
the same module.

Supply-chain integrity
----------------------
Every entry in :data:`_PINNED` MUST carry a real 64-character lowercase
hex SHA256 digest from the upstream release's checksum manifest. Sentinel
placeholder strings of the form ``"<sha256-placeholder-...>"`` are
recognized by :func:`_is_placeholder_sha` and cause
:class:`BinCacheIntegrityError` (a :class:`RuntimeError` subclass) to be
raised at verification time — a loud hard-fail that supersedes the
previous behavior of silently accepting placeholder digests.

Operators MUST replace every placeholder with a real digest before the
corresponding ``(tool, os, arch)`` can be fetched in production.
``BinCacheIntegrityError`` is intentionally NOT caught by the atomic
install's best-effort ``OSError`` cleanup paths — a misconfigured pin is
a policy violation, not a recoverable IO failure, and it must propagate
to the caller.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import importlib
import os
import tarfile
import time
import urllib.error
import urllib.request

# ``platform`` is imported via importlib to hide it from the legacy
# AST-based import-graph builder (autofix.platform.build_import_graph),
# which stem-matches ``import platform`` against the repo-local
# ``autofix/platform.py`` and produces a false-positive edge. Mirrors
# the same workaround adopted by autofix/languages/python.py.
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


class BinCacheIntegrityError(RuntimeError):
    """Raised when a pinned SHA256 is a sentinel placeholder (AC #2).

    Subclasses :class:`RuntimeError` (not :class:`BinaryIntegrityError`)
    because placeholder gating is a *configuration / policy* defect in
    :data:`_PINNED`, distinct from a runtime checksum mismatch on real
    bytes. This exception intentionally propagates past the atomic-install
    path's best-effort ``OSError`` swallowers: a misconfigured pin must
    not be silently downgraded.
    """


def _is_placeholder_sha(value: str) -> bool:
    """Return ``True`` iff ``value`` is a sentinel placeholder digest (AC #1).

    A placeholder is any string that starts with ``"<sha256-placeholder"``
    and ends with ``">"``. Real 64-character lowercase hex digests never
    match this pattern.
    """
    return value.startswith("<sha256-placeholder") and value.endswith(">")


# Pinned (version, sha256) table keyed by (tool, os, arch).
#
# scip-go: SHA256s are the digests of the upstream release archives
# (``.tar.gz``) as published by ``github.com/scip-code/scip-go``. The
# extract step pulls the bare ``scip-go`` binary out of the archive
# before the atomic-install rename. Real digests are pinned per
# upstream release; bumping the version requires re-fetching all
# three asset SHA256s from
# ``https://github.com/scip-code/scip-go/releases/download/v<X>/<asset>.sha256``.
#
# scip-typescript: NOT pinned here. Sourcegraph publishes scip-typescript
# exclusively via npm (``@sourcegraph/scip-typescript``); there are no
# binary release assets to download. ``ensure_binary("scip-typescript")``
# raises BinaryUnavailableError with reason="no_pinned_release"; the
# JSTSAdapter degrades to a non-precision path. See README for the
# manual install steps.
_PINNED: dict[tuple[str, str, str], tuple[str, str]] = {
    ("scip-go", "darwin", "arm64"): (
        "0.2.4",
        "3319187587ec339f18d0331380c4e539388ffe7aaad3fee98952f4b300a593c2",
    ),
    ("scip-go", "linux", "x86_64"): (
        "0.2.4",
        "e2bb0c99af2c0955444543a2114a80f15b7d7762963b7ad8eb2e0c8758eabedd",
    ),
    ("scip-go", "linux", "arm64"): (
        "0.2.4",
        "b257c3eb0356b0f7a32d499e15020c2e93a9273d6863c236c23100bbe719ac25",
    ),
}

# Tools whose release asset is a ``.tar.gz`` archive containing the
# bare binary (extract step required after SHA256-verifying the
# archive). Tools NOT listed here are downloaded as bare binaries.
_ARCHIVED_TOOLS: frozenset[str] = frozenset({"scip-go"})

# Supported platform tuples (AC #23).
_SUPPORTED_PLATFORMS: frozenset[tuple[str, str]] = frozenset(
    {
        ("darwin", "arm64"),
        ("darwin", "x86_64"),
        ("linux", "x86_64"),
        ("linux", "arm64"),
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
    """Return cache root; honors ``AUTOFIX_BIN_CACHE`` (AC #28)."""
    env = os.environ.get("AUTOFIX_BIN_CACHE")
    if env:
        return Path(env).expanduser()
    return Path("~/.cache/autofix/bin").expanduser()


def _sha256_of_file(path: Path) -> str:
    """Compute SHA256 of ``path`` by streaming 64 KiB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_url(tool: str, version: str, os_name: str, arch: str) -> str:
    """Return the upstream GitHub Releases URL for ``(tool, version, os, arch)``.

    The release-asset arch suffix uses ``amd64`` (not ``x86_64``) on
    upstream conventions. We translate at the URL boundary so the rest
    of the module can keep ``x86_64`` as the canonical key.
    """
    asset_arch = "amd64" if arch == "x86_64" else arch
    if tool == "scip-go":
        # The scip-go repo moved from sourcegraph/scip-go to scip-code/scip-go
        # circa v0.2.x. The upstream is `scip-code` today; the old org is a
        # redirect-only shim.
        return (
            f"https://github.com/scip-code/scip-go/releases/download/"
            f"v{version}/scip-go-{os_name}-{asset_arch}.tar.gz"
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

    Mirrors :meth:`autofix.indexing.scip_index.SCIPIndex._acquire_lock`
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


def _extract_binary_from_archive(
    archive: Path, tool: str, out_path: Path
) -> None:
    """Extract the bare ``tool`` executable from a ``.tar.gz`` ``archive``.

    Searches the archive for a regular-file member whose final path
    component matches ``tool`` (case-sensitive, with or without a
    leading directory). Writes the extracted bytes to ``out_path``.

    Defensive against tar-traversal: only extracts members whose
    resolved relative path contains no ``..`` segments. Members that
    would escape the archive root are skipped silently and the search
    continues.

    Raises
    ------
    KeyError
        No member in the archive matched ``tool``.
    tarfile.TarError
        The archive is malformed or unreadable.
    OSError
        Failed to write ``out_path``.
    """
    with tarfile.open(archive, mode="r:*") as tar:
        for member in tar.getmembers():
            if not member.isreg():
                continue
            # Reject any member whose path contains a parent escape.
            parts = Path(member.name).parts
            if ".." in parts or any(p.startswith("/") for p in parts):
                continue
            if Path(member.name).name != tool:
                continue
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            with open(out_path, "wb") as out_fh:
                while True:
                    chunk = extracted.read(65536)
                    if not chunk:
                        break
                    out_fh.write(chunk)
            return
    raise KeyError(
        f"no member named {tool!r} in archive {archive}"
    )


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

    # AC #2: supply-chain integrity gate. If the pinned digest is still a
    # sentinel placeholder, hard-fail before any cache lookup or download.
    # A placeholder means the operator has not completed the provenance
    # step; silently accepting it would let a freshly-downloaded binary
    # execute without meaningful verification.
    if _is_placeholder_sha(expected_sha):
        raise BinCacheIntegrityError(
            f"pinned sha256 for {tool!r} is a placeholder; "
            f"set a real 64-char lowercase hex sha256 in _PINNED"
        )

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

        # Integrity proven on the downloaded bytes. For tools that ship
        # as ``.tar.gz`` archives, extract the bare binary into a
        # second tmp path before atomic-installing. The SHA256 already
        # verified the archive's authenticity; the extracted binary is
        # transitively trusted.
        if tool in _ARCHIVED_TOOLS:
            extracted_tmp = cache_dir / f"{tool}.extracted.tmp"
            try:
                _extract_binary_from_archive(tmp, tool, extracted_tmp)
            except (tarfile.TarError, KeyError, OSError) as exc:
                # Clean both tmp files on failure.
                for p in (tmp, extracted_tmp):
                    try:
                        p.unlink()
                    except (FileNotFoundError, OSError):
                        pass
                raise BinaryUnavailableError(
                    f"failed to extract {tool!r} from archive: {exc}",
                    reason="extract_failed",
                ) from exc
            # Drop the verified archive; install the extracted binary.
            try:
                tmp.unlink()
            except (FileNotFoundError, OSError):
                pass
            tmp = extracted_tmp

        # Integrity proven — install atomically.
        _atomic_install(tmp, final)
        return final


__all__ = [
    "BinCacheIntegrityError",
    "BinaryIntegrityError",
    "BinaryUnavailableError",
    "ensure_binary",
]
