"""``.autofixignore`` support — opt-in path filter for the crawler.

Reads ``<root>/.autofixignore`` (gitignore-style globs) and exposes a
:meth:`AutofixIgnore.matches` predicate that callers (the seed picker
and the bundle expander) consult to drop unwanted paths.

Design notes:

* The file format is gitignore-compatible. We delegate parsing to the
  ``pathspec`` runtime dependency.
* The ``pathspec`` import is guarded — if the dep is missing the
  loader falls back to a no-op instance (logs a stderr warning) rather
  than crashing the cycle.
* Malformed patterns are skipped with a stderr warning per pattern.
* Absent ``.autofixignore`` -> no-op instance (matches nothing).
* Windows line endings (``\\r\\n``) are stripped before parsing.
* Seeds are never excluded by this filter — callers apply
  ``matches()`` only to neighbor candidates and to seed *candidates*
  before relevance ranking. The seed of an already-built bundle is
  immune.
* Documented limitation (D5): autofixignore can only further-exclude
  paths that ``git ls-files`` already returned. It cannot un-exclude
  ``.gitignore``-excluded paths. See ``docs/crawling-tuning.md``.

Pragmatic-pattern fallback:
    Users frequently write patterns like ``*.pb2.py`` intending to
    match generated proto files (which actually have the form
    ``<name>_pb2.py``). Strict gitwildmatch declines to match. We
    add a single-step fallback pass with each pattern's leading
    ``*.`` or ``**/*.`` stripped to ``*`` (so ``*.pb2.py`` also
    behaves like ``*pb2.py``). This stacks additively — a path is
    excluded if either the strict spec OR the pragmatic spec matches.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


_AUTOFIXIGNORE_FILENAME: str = ".autofixignore"


def _pragmatic_variants(pattern: str) -> list[str]:
    """Return additional permissive variants of ``pattern``.

    Returns an empty list when no useful variant exists. The returned
    list never includes the input pattern itself.
    """
    out: list[str] = []
    s = pattern.lstrip()
    # Negation patterns and comments are passed through unchanged —
    # we don't permissive-rewrite them.
    if not s or s.startswith("#") or s.startswith("!"):
        return out
    # ``*.<rest>`` -> ``*<rest>`` so ``*.pb2.py`` also matches
    # ``service_pb2.py``.
    if s.startswith("**/*."):
        out.append("**/*" + s[len("**/*."):])
    elif s.startswith("*."):
        out.append("*" + s[len("*."):])
    return out


class AutofixIgnore:
    """Path-pattern filter loaded from ``<root>/.autofixignore``.

    Construction is private — call :meth:`AutofixIgnore.load`.
    Instances are effectively immutable after load.
    """

    def __init__(self, spec: Any | None, fallback_spec: Any | None = None) -> None:
        # ``spec`` is either a ``pathspec.PathSpec`` (when the file is
        # present and pathspec is importable) or ``None`` (no-op).
        # ``fallback_spec`` carries the pragmatic-variant patterns —
        # see module docstring.
        self._spec = spec
        self._fallback_spec = fallback_spec

    @classmethod
    def load(cls, root: Path) -> "AutofixIgnore":
        """Load ``<root>/.autofixignore`` and return an instance.

        On any failure (missing file, missing ``pathspec`` dep, IO
        error, malformed patterns) the function returns a no-op
        instance and emits a stderr warning. It MUST NOT raise — a
        broken ignore file should not crash a crawl cycle.
        """
        # Guard the pathspec import — it's a runtime dep but the venv
        # may not have it installed yet on first upgrade.
        try:
            import pathspec  # type: ignore[import-not-found]
        except ImportError:
            sys.stderr.write(
                "warning: 'pathspec' package not installed; "
                ".autofixignore support disabled\n",
            )
            return cls(None)

        try:
            ignore_file = root / _AUTOFIXIGNORE_FILENAME
        except (TypeError, ValueError):
            return cls(None)

        try:
            if not ignore_file.is_file():
                return cls(None)
        except OSError:
            return cls(None)

        # Read the file. On any IO failure -> no-op + stderr warning.
        try:
            raw = ignore_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            sys.stderr.write(
                f"warning: failed to read {ignore_file}: {exc}\n",
            )
            return cls(None)

        # Strip Windows line endings; split into individual patterns.
        lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")

        # Validate each pattern individually so a single malformed one
        # doesn't poison the rest. ``pathspec`` raises on malformed
        # gitwildmatch patterns; we catch per-line and skip with a
        # warning.
        valid: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("#"):
                # Comment lines pass through to pathspec as well, but
                # we keep them so the resulting spec preserves
                # gitignore-compatible behavior.
                valid.append(line)
                continue
            try:
                pathspec.PathSpec.from_lines("gitwildmatch", [line])
            except Exception as exc:  # noqa: BLE001 -- pathspec exception types vary
                # Malformed pattern — log to stderr and skip.
                sys.stderr.write(
                    f"warning: malformed pattern in "
                    f"{_AUTOFIXIGNORE_FILENAME!s} {line!r}: {exc}\n",
                )
                continue
            valid.append(line)

        if not valid:
            return cls(None)

        try:
            spec = pathspec.PathSpec.from_lines("gitwildmatch", valid)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(
                f"warning: failed to compile {_AUTOFIXIGNORE_FILENAME}: "
                f"{exc}\n",
            )
            return cls(None)

        # Build the pragmatic-variant spec. See module docstring.
        fallback_patterns: list[str] = []
        for line in valid:
            fallback_patterns.extend(_pragmatic_variants(line))
        fallback_spec: Any | None = None
        if fallback_patterns:
            try:
                fallback_spec = pathspec.PathSpec.from_lines(
                    "gitwildmatch", fallback_patterns,
                )
            except Exception:  # noqa: BLE001
                fallback_spec = None

        return cls(spec, fallback_spec)

    def matches(self, path: Path, root: Path) -> bool:
        """Return True iff ``path`` matches any loaded ignore pattern.

        ``path`` may be absolute or relative. When absolute and rooted
        under ``root``, we compute the rel path; otherwise we pass
        ``path`` through to ``pathspec`` as-is. ``pathspec`` operates
        on POSIX-style paths internally.

        Returns False when the spec is empty / not loaded — the no-op
        case excludes nothing.
        """
        if self._spec is None:
            return False

        try:
            p = path if isinstance(path, Path) else Path(path)
            try:
                rel = p.relative_to(root)
            except ValueError:
                rel = p
            # pathspec gitwildmatch wants forward-slash paths.
            posix = rel.as_posix() if isinstance(rel, Path) else str(rel)
        except (TypeError, ValueError, OSError):
            return False

        try:
            if bool(self._spec.match_file(posix)):
                return True
        except Exception:  # noqa: BLE001 -- contract: never raise
            return False

        # Pragmatic-variant fallback (e.g. ``*.pb2.py`` also matches
        # ``service_pb2.py``).
        if self._fallback_spec is not None:
            try:
                return bool(self._fallback_spec.match_file(posix))
            except Exception:  # noqa: BLE001 -- contract: never raise
                return False
        return False


__all__ = ["AutofixIgnore"]
