"""Atomic JSON write helper (tmp -> fsync -> os.replace -> dir-fsync) (AC 14-18).

Centralizes the 4-step atomic-install pattern that previously lived as
copies in scheduler.py::_cache_put, bin_cache.py::_atomic_install, and
scip_index.py shard save paths.

This helper intentionally does NOT call ``os.chmod`` (AC #17): callers
that require specific permissions must set them explicitly after this
function returns ``True``. Binary-install callers that need executable
mode should keep using :func:`autofix_next.languages.bin_cache._atomic_install`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def atomic_write_json(
    path: Path,
    payload: dict,
    *,
    fsync_dir: bool = True,
) -> bool:
    """Write ``payload`` as deterministic JSON to ``path`` atomically.

    Steps (AC #14):

    1. Serialize ``payload`` via :func:`json.dumps` with
       ``sort_keys=True`` and ``separators=(",", ":")`` so byte output
       is deterministic wrt dict insertion order (AC #15).
    2. Write bytes to ``<path>.tmp`` via a freshly-opened fd, then
       ``os.fsync`` the fd so the bytes are durable before rename.
    3. Optionally ``fsync`` the parent directory so the tmp directory
       entry is durable (best-effort — some filesystems, notably tmpfs,
       reject ``fsync`` on directory fds; swallow those ``OSError``).
    4. ``os.replace(tmp_path, path)`` — atomic wrt concurrent readers
       on POSIX.
    5. Optionally ``fsync`` the parent directory again so the rename
       is durable (best-effort, same rationale as step 3).

    Parameters
    ----------
    path:
        Final destination. The tmp sibling is ``<path>.tmp``.
    payload:
        JSON-serializable mapping. Non-serializable values propagate as
        :class:`TypeError` (a programmer error, not an IO failure, and
        therefore not silently downgraded to ``False``).
    fsync_dir:
        When ``True`` (default), ``fsync`` the parent directory before
        and after ``os.replace``. Set to ``False`` in hot inner loops
        that can tolerate a small durability window (e.g. per-shard
        writes in an indexer that fsyncs once at end-of-run).

    Returns
    -------
    bool
        ``True`` on success; ``False`` on any :class:`OSError` during
        tmp write, ``fsync``, or ``os.replace`` (AC #14, AC #18). On
        failure, the tmp file is best-effort unlinked — both
        :class:`FileNotFoundError` (already gone) and further
        :class:`OSError` during unlink are swallowed so the caller
        always sees a plain ``False``.

    Notes
    -----
    Does NOT call ``os.chmod`` (AC #17). Callers needing mode bits must
    set them explicitly on ``path`` after this function returns ``True``.
    """
    path = Path(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")

    # Deterministic serialization (AC #15). Do this BEFORE any filesystem
    # work so a payload that cannot be serialized raises TypeError before
    # we create a tmp artifact to clean up. TypeError is a programmer
    # bug, not an IO failure, and must propagate — do not catch it.
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    encoded = serialized.encode("utf-8")

    try:
        # Defensive: ensure the parent exists. Caller is expected to
        # have created it, but a missing dir would surface here as an
        # OSError and (correctly) return False.
        path.parent.mkdir(parents=True, exist_ok=True)

        # Step 2: write tmp bytes + fsync the tmp fd.
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o644,
        )
        try:
            # os.write returns the number of bytes written; loop until
            # ``encoded`` is exhausted so short writes do not silently
            # truncate the payload.
            view = memoryview(encoded)
            total = 0
            while total < len(view):
                written = os.write(fd, view[total:])
                if written <= 0:
                    # A 0-byte or negative return from os.write on a
                    # regular file indicates a broken kernel/FS state;
                    # surface it as OSError so the outer handler
                    # cleans up and returns False.
                    raise OSError(
                        f"os.write returned {written} writing tmp file {tmp_path}"
                    )
                total += written
            os.fsync(fd)
        finally:
            try:
                os.close(fd)
            except OSError:
                # Already-closed fd or EIO on close: the durability
                # guarantee was the prior fsync; swallow so we still
                # get the chance to os.replace below.
                pass

        # Step 3: best-effort fsync of parent dir before rename. Dir
        # fsync failures are non-fatal on filesystems that refuse
        # them (tmpfs, some CI sandboxes).
        if fsync_dir:
            _try_fsync_dir(path.parent)

        # Step 4: atomic rename. This is the step whose OSError would
        # leave the target potentially missing — the outer except
        # catches it and returns False.
        os.replace(str(tmp_path), str(path))

        # Step 5: best-effort fsync of parent dir after rename.
        if fsync_dir:
            _try_fsync_dir(path.parent)

        return True
    except OSError:
        # AC #18: best-effort unlink of tmp, swallow missing-file and
        # further OSError on unlink.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return False


def _try_fsync_dir(directory: Path) -> None:
    """Best-effort ``fsync`` of ``directory``; swallow OSError.

    Some filesystems (tmpfs, certain CI sandboxes) reject ``fsync`` on
    directory descriptors. Rename atomicity is provided by
    :func:`os.replace` regardless; the dir-fsync only promotes the
    rename's crash-consistency window, so a failure here should not
    cause the whole write to fail.
    """
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
    finally:
        try:
            os.close(dir_fd)
        except OSError:
            pass


__all__ = ["atomic_write_json"]
