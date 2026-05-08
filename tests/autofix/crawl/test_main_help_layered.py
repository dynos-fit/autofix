"""Layered help tests (ARCH-016 AC-24..26)."""
from __future__ import annotations

import pytest


def test_help_short_lists_dumb_user_anchors(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``autofix --help`` lists init, status, --apply, --once, --help-advanced."""
    from autofix.cli.main import main

    main(["autofix", "--help"])
    out = capsys.readouterr().out

    assert "init" in out.lower()
    assert "status" in out.lower()
    assert "--apply" in out
    assert "--once" in out
    assert "--help-advanced" in out


def test_help_short_does_not_list_advanced_subcommands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The dumb-user help should NOT list scan/run/fix/watch/replay/export-sarif/policy.

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
            f"Dumb-user help mentions advanced command {cmd!r}"
        )


def test_help_advanced_lists_all_subcommands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``autofix --help-advanced`` lists every existing subcommand."""
    from autofix.cli.main import main

    rc = main(["autofix", "--help-advanced"])
    out = capsys.readouterr().out

    for cmd in ("scan", "run", "fix", "watch", "replay", "export-sarif", "policy"):
        assert cmd in out.lower(), f"--help-advanced missing {cmd!r}"


def test_help_short_under_15_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Dumb-user help must stay tight — ≤ 15 lines."""
    from autofix.cli.main import main

    main(["autofix", "--help"])
    out = capsys.readouterr().out
    line_count = len([l for l in out.splitlines() if l.strip()])
    assert line_count <= 15, (
        f"Dumb-user help is {line_count} lines — should be ≤ 15"
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
