"""Template path resolution for the tiered LLM scheduler.

This module is intentionally pure: it performs filesystem-only path
resolution for prompt templates keyed by ``(rule_id, tier)``. It has
no knowledge of LLM invocation, process execution, or network I/O --
the scheduler consumes the returned :class:`pathlib.Path` and decides
what to do with it.

See ``.dynos/task-20260417-008`` for the design. Acceptance criteria
23, 24, 25, 26, 27.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

__all__ = ["resolve_template"]


# The single (rule_id, tier) pair that inherits the pre-tiered template
# at ``prompts_dir / "unused_import_review.md"``. Keeping this as a
# module-level constant avoids a magic tuple duplicated across two
# branches below.
_LEGACY_PAIR: tuple[str, str] = ("unused-import.intra-file", "small")
_LEGACY_FILENAME: str = "unused_import_review.md"


def resolve_template(
    prompts_dir: Path,
    rule_id: str,
    tier: Literal["small", "large"],
    legacy_override: Path | None,
) -> Path:
    """Resolve the prompt template path for ``(rule_id, tier)``.

    Resolution order:

    1. ``legacy_override`` wins iff it is not ``None`` AND
       ``(rule_id, tier) == ("unused-import.intra-file", "small")``.
    2. Otherwise the default is
       ``prompts_dir / f"{rule_id.replace('.', '_')}__{tier}.md"``;
       return it when it is a regular file.
    3. Otherwise, for the single legacy pair, fall back to
       ``prompts_dir / "unused_import_review.md"`` when that file
       exists.
    4. Otherwise return the default path unchanged; the caller is
       responsible for raising :class:`FileNotFoundError`.

    The function performs no I/O beyond ``Path.is_file`` and never
    mutates the filesystem.
    """
    pair = (rule_id, tier)

    if legacy_override is not None and pair == _LEGACY_PAIR:
        return legacy_override

    slug = rule_id.replace(".", "_")
    default = prompts_dir / f"{slug}__{tier}.md"
    if default.is_file():
        return default

    if pair == _LEGACY_PAIR:
        legacy = prompts_dir / _LEGACY_FILENAME
        if legacy.is_file():
            return legacy

    return default
