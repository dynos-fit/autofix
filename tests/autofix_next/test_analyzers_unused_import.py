"""Tests for autofix_next.analyzers.cheap.unused_import.

5 cases:
  1. used import            → no finding
  2. unused import          → exactly one finding
  3. __all__ re-export      → no finding (listed in __all__ counts as used)
  4. TYPE_CHECKING imports  → documented limitation (may flag; we don't assert)
  5. side-effect import     → documented limitation (may flag; we don't assert)
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _run_analyzer(tmp_path: Path, src: str, filename: str = "sample.py"):
    from autofix_next.analyzers.cheap import unused_import as rule
    from autofix_next.parsing.tree_sitter import parse_file
    from autofix_next.indexing.symbols import build_symbol_table

    target = tmp_path / filename
    target.write_text(src, encoding="utf-8")

    parse_result = parse_file(target)
    symbol_table = build_symbol_table(parse_result)
    return rule.analyze(parse_result, symbol_table)


def test_used_import_produces_no_finding(tmp_path: Path) -> None:
    """A bound import that is referenced in the module emits no finding."""
    src = "import os\n\npath = os.getcwd()\n"
    findings = _run_analyzer(tmp_path, src)
    assert list(findings) == [], f"expected no findings for used import, got {findings!r}"


def test_unused_import_produces_one_finding(tmp_path: Path) -> None:
    """A module-level import that is never referenced yields exactly one finding."""
    src = "import os\n\nx = 1\n"
    findings = list(_run_analyzer(tmp_path, src))
    assert len(findings) == 1, f"expected one finding, got {findings!r}"
    finding = findings[0]
    rule_id = getattr(finding, "rule_id", None) or getattr(finding, "rule", None)
    assert rule_id == "unused-import.intra-file", (
        f"rule_id must be 'unused-import.intra-file', got {rule_id!r}"
    )


def test_all_reexport_suppresses_finding(tmp_path: Path) -> None:
    """A name listed in module __all__ counts as used; no finding is emitted."""
    src = 'import helpers\n\n__all__ = ["helpers"]\n'
    findings = list(_run_analyzer(tmp_path, src))
    assert findings == [], (
        f"__all__ re-export must suppress the finding, got {findings!r}"
    )


def test_type_checking_import_is_documented_limitation(tmp_path: Path) -> None:
    """TYPE_CHECKING imports are a documented known limitation: the rule
    may flag them as unused. We only assert the rule runs without error —
    the false-positive behavior is explicitly allowed by design."""
    src = (
        "from __future__ import annotations\n"
        "from typing import TYPE_CHECKING\n"
        "\n"
        "if TYPE_CHECKING:\n"
        "    import os  # only used in string annotations\n"
        "\n"
        "def f(p: 'os.PathLike') -> None:\n"
        "    return None\n"
    )
    findings = _run_analyzer(tmp_path, src)
    # Must not raise. The result (0 or N findings) is left undefined by the
    # known-limitation contract in design-decisions.md §Non-goals.
    list(findings)


def test_side_effect_import_is_documented_limitation(tmp_path: Path) -> None:
    """Side-effect-only imports (e.g. `import readline`) are a documented
    known limitation. The rule may flag them; we only assert the rule runs."""
    src = "import readline  # enables tab completion by side effect\n\nprint('ready')\n"
    findings = _run_analyzer(tmp_path, src)
    list(findings)
