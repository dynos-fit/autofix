"""The ``autofix-next policy`` subcommand.

Read-only inspector for ``.autofix/autofix-policy.json``. Two flags:

* ``--show``    — pretty-print the policy as sorted-key JSON to stdout.
* ``--validate`` — type-check four known top-level keys; exit 2 on issues.

The flags are mutually exclusive (argparse-enforced); invoking with no
flag exits 2 (AC 9). The policy file is never written to.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

HELP_DESCRIPTION = (
    "Inspect the locked .autofix/autofix-policy.json. --show prints the "
    "policy as sorted-key JSON; --validate runs a structural type check "
    "of known top-level keys (llm_tiered, state_migration, "
    "min_priority_for_llm_triage, index) and exits 2 with one diagnostic "
    "line per issue."
)


# Map: known top-level key -> expected type(s).
_KNOWN_KEYS: dict[str, type | tuple[type, ...]] = {
    "llm_tiered": bool,
    "state_migration": dict,
    "min_priority_for_llm_triage": (int, float),
    "index": dict,
}


def _format_type_name(expected: type | tuple[type, ...]) -> str:
    if isinstance(expected, tuple):
        return " or ".join(t.__name__ for t in expected)
    return expected.__name__


def _validate(policy: Any) -> list[str]:
    """Return a list of diagnostic lines; empty = pass.

    The validator is permissive about unknown top-level keys (forward-
    compatible). Only the four ``_KNOWN_KEYS`` entries are type-checked.
    """
    issues: list[str] = []
    if not isinstance(policy, dict):
        issues.append("policy file does not parse as a JSON object")
        return issues
    for key, expected in _KNOWN_KEYS.items():
        if key not in policy:
            continue
        # bool is a subclass of int, so we need a stricter check for
        # min_priority_for_llm_triage which expects int|float (not bool).
        value = policy[key]
        if expected is bool:
            if not isinstance(value, bool):
                issues.append(
                    f"policy.{key}: expected {_format_type_name(expected)}, "
                    f"got {type(value).__name__}"
                )
        elif expected == (int, float):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                issues.append(
                    f"policy.{key}: expected {_format_type_name(expected)}, "
                    f"got {type(value).__name__}"
                )
        else:
            if not isinstance(value, expected):
                issues.append(
                    f"policy.{key}: expected {_format_type_name(expected)}, "
                    f"got {type(value).__name__}"
                )
    return issues


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register --root and the --show/--validate mutex group (AC 9)."""
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help=(
            "Repo root containing .autofix/autofix-policy.json. "
            "Defaults to the current working directory."
        ),
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--show",
        action="store_true",
        help="Print the policy file as sorted-key JSON to stdout.",
    )
    grp.add_argument(
        "--validate",
        action="store_true",
        help=(
            "Type-check known top-level keys. Exit 0 on pass, 2 on "
            "issues (one diagnostic line per issue)."
        ),
    )


def run(args: argparse.Namespace, *, root: Path | None = None) -> int:
    """Entry point for `autofix-next policy`."""
    effective_root: Path = (
        getattr(args, "root", None)
        or root
        or Path.cwd()
    )
    policy_path = effective_root / ".autofix" / "autofix-policy.json"

    # Load file (shared by --show and --validate paths).
    try:
        raw = policy_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(
            f"autofix-policy.json not found at {policy_path}",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(
            f"autofix-policy.json read error: {exc}",
            file=sys.stderr,
        )
        return 1

    if args.show:
        return _do_show(raw)
    if args.validate:
        return _do_validate(raw)
    # Unreachable — argparse mutex group prevents this case.
    return 2


def _do_show(raw: str) -> int:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"policy file does not parse as JSON: {exc}",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(data, sort_keys=True, indent=2))
    return 0


def _do_validate(raw: str) -> int:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"policy file does not parse as JSON: {exc}",
            file=sys.stderr,
        )
        return 2
    issues = _validate(data)
    if issues:
        for line in issues:
            print(line, file=sys.stderr)
        return 2
    return 0
