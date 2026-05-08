"""Flag-combination rejection tests for `autofix run` (AC-4 / AC-17.c)."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from autofix.cli import run_command


def _ns(**kw) -> argparse.Namespace:
    base = dict(
        root=Path("/tmp/_autofix_test_combinatorics"),
        apply=False,
        suggest=False,
        auto_llm=False,
        analyzers="",
        max_retries=3,
        max_llm_patches=None,
        quiet=True,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_suggest_and_auto_llm_mutually_exclusive(capsys) -> None:
    rc = run_command.run(_ns(suggest=True, auto_llm=True, apply=True))
    assert rc == 2
    err = capsys.readouterr().err
    assert "mutually exclusive" in err
    assert "--suggest" in err
    assert "--auto-llm" in err


def test_auto_llm_requires_apply(capsys) -> None:
    rc = run_command.run(_ns(auto_llm=True, apply=False))
    assert rc == 2
    err = capsys.readouterr().err
    assert "requires --apply" in err


def test_negative_max_retries_rejected(capsys) -> None:
    rc = run_command.run(_ns(max_retries=-1))
    assert rc == 2
    err = capsys.readouterr().err
    assert "non-negative" in err


def test_zero_max_llm_patches_rejected(capsys) -> None:
    rc = run_command.run(_ns(max_llm_patches=0))
    assert rc == 2
    err = capsys.readouterr().err
    assert "positive integer" in err


def test_negative_max_llm_patches_rejected(capsys) -> None:
    rc = run_command.run(_ns(max_llm_patches=-5))
    assert rc == 2
