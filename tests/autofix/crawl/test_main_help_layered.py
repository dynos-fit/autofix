"""Layered help tests (ARCH-016 AC-24..26)."""
from __future__ import annotations

import pytest


def test_help_short_lists_quickstart_anchors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``autofix --help`` lists the primary daemon UX:
    init / start / status / logs / stop, plus ``--once`` for the
    one-shot foreground form and ``--help-advanced`` for the rest.

    ``--apply`` was removed from the quick-start help when ``start``
    became the primary path; it remains available via
    ``--help-advanced`` for operators driving the foreground crawl
    manually.
    """
    from autofix.cli.main import main

    main(["autofix", "--help"])
    out = capsys.readouterr().out

    for anchor in ("init", "start", "status", "logs", "stop"):
        assert anchor in out.lower(), f"missing quick-start anchor: {anchor!r}"
    assert "--once" in out
    assert "--help-advanced" in out


def test_help_short_does_not_list_advanced_subcommands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The quick-start help should NOT list scan/run/fix/watch/replay/export-sarif/policy.

    Those commands stay accessible (test_main_bare_dispatches_to_crawl
    confirms ``autofix scan --help`` works), they just don't appear in
    the bare help screen.
    """
    from autofix.cli.main import main

    main(["autofix", "--help"])
    out = capsys.readouterr().out

    advanced = ["scan", "run", "fix", "watch", "replay", "export-sarif", "policy"]
    # We exempt mentions like "find" or "scan" inside prose. Only flag
    # them if they appear as standalone command words in a usage block.
    # Conservative check: ensure none appear in a list-of-commands form.
    for cmd in advanced:
        assert f"  autofix {cmd}" not in out, (
            f"Quick-start help mentions advanced command {cmd!r}"
        )


def test_help_advanced_lists_all_subcommands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``autofix --help-advanced`` lists every existing subcommand."""
    from autofix.cli.main import main

    main(["autofix", "--help-advanced"])
    out = capsys.readouterr().out

    for cmd in ("scan", "run", "fix", "watch", "replay", "export-sarif", "policy"):
        assert cmd in out.lower(), f"--help-advanced missing {cmd!r}"


def test_help_short_under_15_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Quick-start help must stay tight — ≤ 15 lines."""
    from autofix.cli.main import main

    main(["autofix", "--help"])
    out = capsys.readouterr().out
    line_count = len([line for line in out.splitlines() if line.strip()])
    assert line_count <= 15, (
        f"Quick-start help is {line_count} lines — should be ≤ 15"
    )


def test_subcommand_help_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    """``autofix scan --help`` (and other subcommands) keeps full flag list."""
    from autofix.cli.main import main

    # argparse exits via SystemExit on --help; catch it.
    try:
        main(["autofix", "scan", "--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    # scan has --root, --full-sweep — both should show up.
    assert "--root" in out


def test_debug_crawl_flag_appears_in_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """AC 22: ``--debug-crawl`` flag must appear in the CLI help text.

    The help output for the top-level parser (or the advanced help)
    must include ``--debug-crawl`` so operators can discover it.
    """
    from autofix.cli.main import main

    # Try the advanced help first (most likely location for a debug flag)
    try:
        main(["autofix", "--help-advanced"])
    except SystemExit:
        pass
    out_advanced = capsys.readouterr().out

    # Also try bare help
    main(["autofix", "--help"])
    out_basic = capsys.readouterr().out

    combined = out_advanced + out_basic
    assert "--debug-crawl" in combined, (
        "--debug-crawl flag must appear in autofix help output (--help or --help-advanced)"
    )
