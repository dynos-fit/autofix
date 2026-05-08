"""Bare ``autofix`` dispatches to the crawl driver (ARCH-016 AC-19..21)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


def test_bare_autofix_no_root_prints_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``autofix`` with no subcommand and no --root prints help."""
    from autofix.cli.main import main

    rc = main(["autofix"])
    out = capsys.readouterr().out
    # Must include at least one of the dumb-user-help anchors.
    assert (
        "init" in out.lower()
        or "status" in out.lower()
        or "find and fix" in out.lower()
    )
    assert rc == 0


def test_bare_autofix_with_root_dispatches_to_crawl(tmp_path: Path) -> None:
    """``autofix --root .`` runs the crawl driver."""
    from autofix.cli import main as main_mod

    captured = {}

    def _fake_continuous(*, root, mode, budget, interval_seconds, quiet):
        captured["root"] = root
        captured["mode"] = mode
        captured["budget"] = budget
        return 0

    with patch(
        "autofix.crawl.driver.run_crawl_continuously",
        side_effect=_fake_continuous,
    ):
        rc = main_mod.main(["autofix", "--root", str(tmp_path)])

    assert rc == 0
    assert captured["root"] == tmp_path


def test_bare_autofix_once_runs_one_cycle(tmp_path: Path) -> None:
    """``autofix --root . --once`` invokes ``run_crawl_once``, not the loop."""
    from autofix.cli import main as main_mod

    one_called = {"flag": False}
    cont_called = {"flag": False}

    def _once(**_kwargs):
        one_called["flag"] = True
        return 0

    def _cont(**_kwargs):
        cont_called["flag"] = True
        return 0

    with patch("autofix.crawl.driver.run_crawl_once", side_effect=_once), \
         patch("autofix.crawl.driver.run_crawl_continuously", side_effect=_cont):
        rc = main_mod.main(["autofix", "--root", str(tmp_path), "--once"])

    assert rc == 0
    assert one_called["flag"] is True
    assert cont_called["flag"] is False


def test_bare_autofix_apply_overrides_preview_mode(tmp_path: Path) -> None:
    """``--apply`` upgrades a config-default ``preview`` mode to a non-preview."""
    import json
    from autofix.cli import main as main_mod

    cfg = tmp_path / ".autofix"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({
        "version": 1, "mode": "preview", "budget": "cheap"
    }))

    captured = {}

    def _capture_mode(**kwargs):
        captured["mode"] = kwargs["mode"]
        return 0

    with patch("autofix.crawl.driver.run_crawl_continuously", side_effect=_capture_mode):
        main_mod.main(["autofix", "--root", str(tmp_path), "--apply"])

    # --apply forces a non-preview mode.
    assert captured["mode"] != "preview"


def test_bare_autofix_uses_config_when_present(tmp_path: Path) -> None:
    """The dispatcher reads ``.autofix/config.json`` for mode + budget."""
    import json
    from autofix.cli import main as main_mod

    cfg = tmp_path / ".autofix"
    cfg.mkdir()
    (cfg / "config.json").write_text(json.dumps({
        "version": 1, "mode": "pr", "budget": "aggressive"
    }))

    captured = {}

    def _capture(**kwargs):
        captured["mode"] = kwargs["mode"]
        captured["budget"] = kwargs["budget"]
        return 0

    with patch("autofix.crawl.driver.run_crawl_continuously", side_effect=_capture):
        main_mod.main(["autofix", "--root", str(tmp_path)])

    assert captured["mode"] == "pr"
    assert captured["budget"] == "aggressive"


def test_existing_subcommands_still_work(tmp_path: Path) -> None:
    """An explicit subcommand (``scan``, ``run``, etc) takes precedence
    and does NOT route to the crawl."""
    from autofix.cli import main as main_mod

    crawl_called = {"flag": False}

    def _crawl_called(**_kwargs):
        crawl_called["flag"] = True
        return 0

    with patch(
        "autofix.crawl.driver.run_crawl_continuously",
        side_effect=_crawl_called,
    ):
        # ``scan --help`` triggers argparse's SystemExit(0); catch it.
        try:
            main_mod.main(["autofix", "scan", "--help"])
        except SystemExit as exc:
            rc = exc.code if exc.code is not None else 0
    assert rc == 0
    assert crawl_called["flag"] is False


def test_init_subcommand_dispatches_to_init_command(tmp_path: Path) -> None:
    from autofix.cli import main as main_mod

    init_called = {"flag": False}

    def _fake_init(*, default_root):
        init_called["flag"] = True
        return 0

    with patch("autofix.cli.init_command.run_init", side_effect=_fake_init):
        main_mod.main(["autofix", "init", "--root", str(tmp_path)])
    assert init_called["flag"] is True


def test_status_subcommand_dispatches(tmp_path: Path) -> None:
    from autofix.cli import main as main_mod

    status_called = {"flag": False}

    def _fake_status(*, root):
        status_called["flag"] = True
        return 0

    with patch("autofix.cli.status_command.run_status", side_effect=_fake_status):
        main_mod.main(["autofix", "status", "--root", str(tmp_path)])
    assert status_called["flag"] is True
