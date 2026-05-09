"""Driver exception-isolation contract (ARCH-016 AC-18)."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


def _git_init(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    (tmp_path / "a.py").write_text("# a\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)


def test_continuous_loop_continues_on_cycle_exception(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cycle that raises Exception is logged; loop continues to next."""
    from autofix.crawl import driver

    _git_init(tmp_path)

    cycle_count = {"n": 0}

    def _flaky_cycle(**_kwargs):
        cycle_count["n"] += 1
        if cycle_count["n"] == 1:
            raise RuntimeError("boom — cycle 1")
        # Cycle 2 returns OK and asks the loop to stop via interrupt.
        raise KeyboardInterrupt

    with patch("autofix.crawl.driver._run_crawl_once_body", side_effect=_flaky_cycle), \
         patch("autofix.crawl.driver._sleep", lambda _: None):
        rc = driver.run_crawl_continuously(
            root=tmp_path,
            mode="preview",
            budget="cheap",
            interval_seconds=1,
            quiet=False,
        )

    assert rc == 0
    assert cycle_count["n"] == 2  # First raised, loop continued, second exited.
    err = capsys.readouterr().err
    # The loop should have logged the cycle-1 error before continuing.
    assert "boom" in err or "RuntimeError" in err


def test_keyboard_interrupt_propagates(tmp_path: Path) -> None:
    """KeyboardInterrupt is NOT caught by the per-cycle handler — it
    propagates to the loop's outer handler and returns 0."""
    from autofix.crawl import driver

    _git_init(tmp_path)

    with patch("autofix.crawl.driver._run_crawl_once_body", side_effect=KeyboardInterrupt), \
         patch("autofix.crawl.driver._sleep", lambda _: None):
        rc = driver.run_crawl_continuously(
            root=tmp_path,
            mode="preview",
            budget="cheap",
            interval_seconds=1,
            quiet=True,
        )
    assert rc == 0


def test_system_exit_propagates(tmp_path: Path) -> None:
    """SystemExit is NOT caught by the per-cycle handler."""
    from autofix.crawl import driver

    _git_init(tmp_path)

    with patch("autofix.crawl.driver._run_crawl_once_body", side_effect=SystemExit(3)), \
         patch("autofix.crawl.driver._sleep", lambda _: None):
        with pytest.raises(SystemExit) as exc:
            driver.run_crawl_continuously(
                root=tmp_path,
                mode="preview",
                budget="cheap",
                interval_seconds=1,
                quiet=True,
            )
    assert exc.value.code == 3


def test_pidfile_written_and_removed(tmp_path: Path) -> None:
    """Driver writes ``.autofix/crawl.pid`` on start, removes on exit."""
    from autofix.crawl import driver

    _git_init(tmp_path)
    pidfile = tmp_path / ".autofix" / "crawl.pid"

    captured_pid = {"value": None}

    def _capture_then_quit(**_kwargs):
        captured_pid["value"] = pidfile.read_text() if pidfile.exists() else None
        raise KeyboardInterrupt

    with patch("autofix.crawl.driver._run_crawl_once_body", side_effect=_capture_then_quit), \
         patch("autofix.crawl.driver._sleep", lambda _: None):
        rc = driver.run_crawl_continuously(
            root=tmp_path,
            mode="preview",
            budget="cheap",
            interval_seconds=1,
            quiet=True,
        )
    assert rc == 0

    # During the cycle, the pidfile existed and held our PID.
    import os
    assert captured_pid["value"] is not None
    assert int(captured_pid["value"]) == os.getpid()

    # After the loop exited, the pidfile is gone.
    assert not pidfile.exists()
