"""``autofix init`` interactive wizard tests (ARCH-016 AC-22)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


def test_init_writes_canonical_config(tmp_path: Path) -> None:
    """Default answers (just press enter) → balanced + pr."""
    from autofix.cli.init_command import run_init

    answers = iter(["", "", str(tmp_path)])  # accept all defaults
    with patch("builtins.input", lambda *_a, **_k: next(answers)):
        rc = run_init(default_root=tmp_path)
    assert rc == 0

    cfg_path = tmp_path / ".autofix" / "config.json"
    assert cfg_path.exists()
    cfg = json.loads(cfg_path.read_text())
    assert cfg["version"] == 1
    assert cfg["mode"] == "pr"
    assert cfg["budget"] == "balanced"


def test_init_accepts_explicit_choice(tmp_path: Path) -> None:
    """Operator picks preview + cheap → those land in the config."""
    from autofix.cli.init_command import run_init

    answers = iter(["preview", "cheap", str(tmp_path)])
    with patch("builtins.input", lambda *_a, **_k: next(answers)):
        rc = run_init(default_root=tmp_path)
    assert rc == 0

    cfg = json.loads((tmp_path / ".autofix" / "config.json").read_text())
    assert cfg["mode"] == "preview"
    assert cfg["budget"] == "cheap"


def test_init_rejects_unknown_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Unknown mode answer → error message, exit 2, no file written."""
    from autofix.cli.init_command import run_init

    answers = iter(["nonsense"])
    with patch("builtins.input", lambda *_a, **_k: next(answers)):
        rc = run_init(default_root=tmp_path)
    assert rc == 2
    err = capsys.readouterr().err
    assert "mode" in err.lower()
    assert not (tmp_path / ".autofix" / "config.json").exists()


def test_init_rejects_unknown_budget(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from autofix.cli.init_command import run_init

    answers = iter(["pr", "wat"])
    with patch("builtins.input", lambda *_a, **_k: next(answers)):
        rc = run_init(default_root=tmp_path)
    assert rc == 2
    err = capsys.readouterr().err
    assert "budget" in err.lower()


def test_init_creates_dotautofix_dir(tmp_path: Path) -> None:
    """``init`` creates ``.autofix/`` if it doesn't already exist."""
    from autofix.cli.init_command import run_init

    target = tmp_path / "fresh-repo"
    target.mkdir()
    answers = iter(["", "", str(target)])
    with patch("builtins.input", lambda *_a, **_k: next(answers)):
        rc = run_init(default_root=target)
    assert rc == 0
    assert (target / ".autofix").is_dir()
    assert (target / ".autofix" / "config.json").is_file()


def test_init_overwrites_existing_config(tmp_path: Path) -> None:
    """Re-running ``init`` against an existing config overwrites it."""
    from autofix.cli.init_command import run_init

    cfg_dir = tmp_path / ".autofix"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps(
        {"version": 1, "mode": "preview", "budget": "cheap"}
    ))

    answers = iter(["pr", "aggressive", str(tmp_path)])
    with patch("builtins.input", lambda *_a, **_k: next(answers)):
        rc = run_init(default_root=tmp_path)
    assert rc == 0

    cfg = json.loads((cfg_dir / "config.json").read_text())
    assert cfg["mode"] == "pr"
    assert cfg["budget"] == "aggressive"
