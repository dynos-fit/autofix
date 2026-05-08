"""``autofix init`` — interactive 3-question wizard (ARCH-016 AC-22)."""
from __future__ import annotations

import sys
from pathlib import Path

from autofix.crawl.config import write_config


_PROMPT_MODE = (
    "What should autofix do?\n"
    "  [1] Find bugs and open PRs for fixes (recommended)\n"
    "  [2] Find bugs but never modify code\n"
    "  [3] Find bugs and commit fixes directly to current branch\n"
    "Choice (default: 1): "
)
_PROMPT_BUDGET = (
    "How much should it spend per day?\n"
    "  [1] Cheap        (~$0.50/day, 1 bundle/hour)\n"
    "  [2] Balanced     (~$2/day,    5 bundles/hour) (recommended)\n"
    "  [3] Aggressive   (~$10/day,   20 bundles/hour)\n"
    "Choice (default: 2): "
)
_PROMPT_ROOT = "Repository root (default: {default}): "


_MODE_CHOICES = {"1": "pr", "2": "preview", "3": "commit",
                 "pr": "pr", "preview": "preview", "commit": "commit"}
_BUDGET_CHOICES = {"1": "cheap", "2": "balanced", "3": "aggressive",
                   "cheap": "cheap", "balanced": "balanced",
                   "aggressive": "aggressive"}


def run_init(*, default_root: Path) -> int:
    """Run the wizard and write ``.autofix/config.json``.

    Returns 0 on success, 2 on a parse error (unknown mode or budget).
    """
    # Mode question — empty input means default ("1" = pr).
    raw_mode = input(_PROMPT_MODE).strip().lower()
    if raw_mode == "":
        mode = "pr"
    elif raw_mode in _MODE_CHOICES:
        mode = _MODE_CHOICES[raw_mode]
    else:
        print(
            f"autofix: unknown mode {raw_mode!r}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    # Budget question — empty input means default ("2" = balanced).
    raw_budget = input(_PROMPT_BUDGET).strip().lower()
    if raw_budget == "":
        budget = "balanced"
    elif raw_budget in _BUDGET_CHOICES:
        budget = _BUDGET_CHOICES[raw_budget]
    else:
        print(
            f"autofix: unknown budget {raw_budget!r}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    # Root question — empty input means the supplied default.
    raw_root = input(_PROMPT_ROOT.format(default=default_root)).strip()
    root = Path(raw_root) if raw_root else Path(default_root)

    cfg_path = write_config(root, mode=mode, budget=budget)
    print(f"autofix: wrote {cfg_path}")
    return 0


__all__ = ["run_init"]
