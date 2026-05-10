"""Tests for ``autofix.llm.triage``.

Covers acceptance criteria 23, 24, 25, 26, 27, 29, 31 from
task-20260417-008:

  AC 23 — ``resolve_template(prompts_dir, rule_id, tier, legacy_override)``
          signature; no ``autofix.llm_backend``, ``subprocess``, or HTTP
          client imports in ``triage.py``.
  AC 24 — default path is ``prompts_dir / f"{slug}__{tier}.md"`` where
          ``slug = rule_id.replace('.', '_')``.
  AC 25 — ``legacy_override`` is honored ONLY for
          ``(rule_id, tier) == ("unused-import.intra-file", "small")``.
  AC 26 — legacy fallback: when default-per-tier file does NOT exist on
          disk AND ``prompts_dir / "unused_import_review.md"`` exists AND
          ``(rule_id, tier) == ("unused-import.intra-file", "small")``,
          return the legacy path.
  AC 27 — when nothing exists, the resolver returns the default
          (non-existent) path; the caller raises ``FileNotFoundError``.
  AC 29 — ``autofix/llm/prompts/unused-import_intra-file__large.md``
          exists, non-empty UTF-8.
  AC 31 — ``triage.py`` does not contain the substrings ``run_prompt``
          or ``claude``.

AC 30 (and the report_writer.py / ReportRecord coverage tied to it)
was removed when the unused report_writer module was deleted —
PROACTIVE-06 from the proactive meta-audit.
"""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TRIAGE_PATH = REPO_ROOT / "autofix" / "llm" / "triage.py"
PROMPTS_DIR = REPO_ROOT / "autofix" / "llm" / "prompts"
LARGE_TEMPLATE = PROMPTS_DIR / "unused-import_intra-file__large.md"


# ---------------------------------------------------------------------------
# AC 23 — resolve_template signature + grep prohibitions.
# ---------------------------------------------------------------------------


def test_resolve_template_signature_and_returns_path():
    """AC 23: ``resolve_template`` is importable and returns a ``Path``."""
    from autofix.llm.triage import resolve_template

    assert callable(resolve_template)
    # Pure path resolution; no side effects, no LLM calls.
    p = resolve_template(
        PROMPTS_DIR, "unused-import.intra-file", "small", None
    )
    assert isinstance(p, Path), (
        f"resolve_template must return a Path: got {type(p).__name__}"
    )


def test_triage_py_has_no_run_prompt_or_claude():
    """AC 31: ``triage.py`` contains neither ``run_prompt`` nor ``claude``."""
    assert TRIAGE_PATH.is_file(), (
        f"triage.py must exist at {TRIAGE_PATH}"
    )
    contents = TRIAGE_PATH.read_text(encoding="utf-8")
    assert "run_prompt" not in contents, (
        "triage.py must not contain the substring 'run_prompt'"
    )
    assert "claude" not in contents, (
        "triage.py must not contain the substring 'claude'"
    )


def test_triage_py_does_not_import_autofix_llm_backend_or_subprocess():
    """AC 23: ``triage.py`` does not import ``autofix.llm_backend`` or ``subprocess``."""
    contents = TRIAGE_PATH.read_text(encoding="utf-8")
    assert "autofix.llm_backend" not in contents, (
        "triage.py must not import from autofix.llm_backend"
    )
    assert "import subprocess" not in contents, (
        "triage.py must not import subprocess"
    )
    # No HTTP client.
    for banned in ("urllib.request", "httpx", "requests"):
        assert banned not in contents, (
            f"triage.py must not reference HTTP client {banned!r}"
        )


# ---------------------------------------------------------------------------
# AC 24 — default path convention.
# ---------------------------------------------------------------------------


def test_resolve_template_default_small_path(tmp_path):
    """AC 24: default path is ``prompts_dir / "{rule_slug}__{tier}.md"``."""
    from autofix.llm.triage import resolve_template

    slug_file = tmp_path / "my-rule_sub__small.md"
    slug_file.write_text("stub template", encoding="utf-8")

    p = resolve_template(tmp_path, "my-rule.sub", "small", None)
    assert p == slug_file, (
        f"default path resolution incorrect: got {p!r}"
    )


def test_resolve_template_default_large_path(tmp_path):
    """AC 24: default path convention applies for ``tier="large"``."""
    from autofix.llm.triage import resolve_template

    slug_file = tmp_path / "my-rule_sub__large.md"
    slug_file.write_text("stub template", encoding="utf-8")

    p = resolve_template(tmp_path, "my-rule.sub", "large", None)
    assert p == slug_file


# ---------------------------------------------------------------------------
# AC 25 — legacy_override is honored ONLY for (unused-import.intra-file, small).
# ---------------------------------------------------------------------------


def test_legacy_override_honored_for_unused_import_small(tmp_path):
    """AC 25: legacy_override wins for (``unused-import.intra-file``, ``"small"``)."""
    from autofix.llm.triage import resolve_template

    override = tmp_path / "custom_override.md"
    override.write_text("overridden", encoding="utf-8")

    p = resolve_template(tmp_path, "unused-import.intra-file", "small", override)
    assert p == override


def test_legacy_override_ignored_for_different_rule(tmp_path):
    """AC 25: ``legacy_override`` is IGNORED for any other ``(rule_id, tier)``."""
    from autofix.llm.triage import resolve_template

    override = tmp_path / "custom_override.md"
    override.write_text("overridden", encoding="utf-8")

    # Different rule_id → override ignored.
    p = resolve_template(tmp_path, "other-rule.whatever", "small", override)
    assert p != override, (
        f"legacy_override must be ignored for other rule_id: got {p!r}"
    )


def test_legacy_override_ignored_for_large_tier(tmp_path):
    """AC 25: ``legacy_override`` is IGNORED for the large tier even on the legacy rule."""
    from autofix.llm.triage import resolve_template

    override = tmp_path / "custom_override.md"
    override.write_text("overridden", encoding="utf-8")

    p = resolve_template(tmp_path, "unused-import.intra-file", "large", override)
    assert p != override, (
        f"legacy_override must be ignored for large tier: got {p!r}"
    )


# ---------------------------------------------------------------------------
# AC 26 — legacy fallback.
# ---------------------------------------------------------------------------


def test_legacy_fallback_when_default_missing(tmp_path):
    """AC 26: legacy ``unused_import_review.md`` used when default-small is absent."""
    from autofix.llm.triage import resolve_template

    legacy = tmp_path / "unused_import_review.md"
    legacy.write_text("legacy", encoding="utf-8")
    # No `unused-import_intra-file__small.md` on disk.

    p = resolve_template(tmp_path, "unused-import.intra-file", "small", None)
    assert p == legacy, (
        f"legacy fallback must return {legacy!r}: got {p!r}"
    )


def test_legacy_fallback_skipped_when_default_exists(tmp_path):
    """AC 26: default path wins when it exists, even if legacy also exists."""
    from autofix.llm.triage import resolve_template

    legacy = tmp_path / "unused_import_review.md"
    legacy.write_text("legacy", encoding="utf-8")
    default_path = tmp_path / "unused-import_intra-file__small.md"
    default_path.write_text("new default", encoding="utf-8")

    p = resolve_template(tmp_path, "unused-import.intra-file", "small", None)
    assert p == default_path, (
        f"default must win over legacy when both exist: got {p!r}"
    )


def test_legacy_fallback_not_for_large_tier(tmp_path):
    """AC 26: legacy fallback does NOT apply to the large tier."""
    from autofix.llm.triage import resolve_template

    legacy = tmp_path / "unused_import_review.md"
    legacy.write_text("legacy", encoding="utf-8")

    p = resolve_template(tmp_path, "unused-import.intra-file", "large", None)
    assert p != legacy, (
        f"legacy fallback must not apply for large tier: got {p!r}"
    )


# ---------------------------------------------------------------------------
# AC 27 — non-existent default returned when nothing matches.
# ---------------------------------------------------------------------------


def test_resolve_template_returns_nonexistent_default_when_nothing_matches(tmp_path):
    """AC 27: when no rule triggers, return the default path (which may not exist).

    The caller (``Scheduler._resolve_template_bytes``) is responsible for
    raising ``FileNotFoundError``; this helper is pure.
    """
    from autofix.llm.triage import resolve_template

    expected = tmp_path / "some-rule_sub__small.md"
    # Do NOT create the file.

    p = resolve_template(tmp_path, "some-rule.sub", "small", None)
    assert p == expected


# ---------------------------------------------------------------------------
# AC 29 — large template ships.
# ---------------------------------------------------------------------------


def test_large_tier_template_exists_and_is_nonempty_utf8():
    """AC 29: the new ``unused-import_intra-file__large.md`` template ships."""
    assert LARGE_TEMPLATE.is_file(), (
        f"large-tier template must exist at {LARGE_TEMPLATE}"
    )
    text = LARGE_TEMPLATE.read_text(encoding="utf-8")
    assert text.strip() != "", (
        f"large-tier template must be non-empty: {LARGE_TEMPLATE}"
    )

