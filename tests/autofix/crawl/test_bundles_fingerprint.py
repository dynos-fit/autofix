"""Bundle fingerprint canonicalization (ARCH-016 AC-6)."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autofix.crawl.bundles import Bundle


def _bundle(seed: str, files: list[str]) -> "Bundle":
    """Helper: construct a Bundle with the given files."""
    from autofix.crawl.bundles import Bundle

    file_paths = tuple(Path(f) for f in files)
    total_bytes = sum(len(f) for f in files)  # placeholder
    return Bundle(
        seed_path=Path(seed),
        file_paths=file_paths,
        total_bytes=total_bytes,
        fingerprint=Bundle.compute_fingerprint(file_paths),
    )


def test_same_files_same_fingerprint() -> None:
    b1 = _bundle("a.py", ["a.py", "b.py", "c.py"])
    b2 = _bundle("a.py", ["a.py", "b.py", "c.py"])
    assert b1.fingerprint == b2.fingerprint


def test_files_in_different_order_same_fingerprint() -> None:
    """Fingerprint canonicalizes via sort — order shouldn't matter."""
    b1 = _bundle("a.py", ["a.py", "b.py", "c.py"])
    b2 = _bundle("a.py", ["c.py", "a.py", "b.py"])
    assert b1.fingerprint == b2.fingerprint


def test_different_files_different_fingerprint() -> None:
    b1 = _bundle("a.py", ["a.py", "b.py"])
    b2 = _bundle("a.py", ["a.py", "c.py"])
    assert b1.fingerprint != b2.fingerprint


def test_different_seed_same_files_same_fingerprint() -> None:
    """Fingerprint depends on the file SET, not the seed identity.

    Two bundles with identical file sets but different seeds resolve
    to the same fingerprint — they ask the same question of the same
    code, just from different angles. The (fingerprint, analyzer)
    cache key in the LLM cache handles that.
    """
    b1 = _bundle("a.py", ["a.py", "b.py", "c.py"])
    b2 = _bundle("c.py", ["a.py", "b.py", "c.py"])
    assert b1.fingerprint == b2.fingerprint


def test_fingerprint_is_64_char_hex() -> None:
    import re

    b = _bundle("a.py", ["a.py", "b.py"])
    assert re.fullmatch(r"[0-9a-f]{64}", b.fingerprint), b.fingerprint


def test_singleton_bundle_fingerprint() -> None:
    """A single-file bundle has a stable fingerprint."""
    b1 = _bundle("a.py", ["a.py"])
    b2 = _bundle("a.py", ["a.py"])
    assert b1.fingerprint == b2.fingerprint
    assert len(b1.fingerprint) == 64


def test_bundle_is_frozen() -> None:
    """Bundle dataclass must be frozen — fingerprint is immutable."""
    import dataclasses
    from autofix.crawl.bundles import Bundle

    assert dataclasses.is_dataclass(Bundle)
    b = _bundle("a.py", ["a.py", "b.py"])
    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        b.fingerprint = "tampered"  # type: ignore[misc]
