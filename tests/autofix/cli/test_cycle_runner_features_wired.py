"""Pin that ``_run_crawl_once_body`` actually wires every flag.

Closes the gap surfaced 2026-05-09: PR #84 added six capability
surfaces to the crawler — file classifier, supplemental scoring,
class-aware expansion, .autofixignore, impact-cone, and debug-crawl
— but only two were threaded into the cycle body's actual call to
``pick_next_batch``. The other four were dead at the wiring layer:
the modules existed, were imported, were tested in isolation, but
production never constructed them.

This module asserts the wire-up by patching ``pick_next_batch`` and
inspecting the kwargs the cycle body actually passes.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from autofix.crawl.bundles import ClassAwareConfig
from autofix.crawl.score import ScoringFlags


def _make_repo(tmp_path: Path, config: dict | None = None) -> Path:
    """Create a minimal git-shaped tmp_path. Optionally pre-seed
    ``.autofix/config.json`` so the cycle body's ``read_crawler_flags``
    call returns specific flag values.
    """
    autofix_dir = tmp_path / ".autofix"
    autofix_dir.mkdir(parents=True)
    if config is not None:
        (autofix_dir / "config.json").write_text(json.dumps(config))
    return tmp_path


def test_default_config_passes_none_for_all_optional_flags(
    tmp_path: Path,
) -> None:
    """When .autofix/config.json has no ``crawler`` section,
    ``pick_next_batch`` is called with byte-identity-safe defaults:
    autofixignore is the no-op AutofixIgnore, scoring_flags is None,
    class_aware_config is None.
    """
    from autofix.cli.cycle_runner import _run_crawl_once_body

    _make_repo(tmp_path, config={"version": 1, "mode": "preview", "budget": "cheap"})

    captured = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return []

    with patch("autofix.crawl.picker.pick_next_batch", side_effect=_fake):
        _run_crawl_once_body(
            root=tmp_path, mode="preview", budget="cheap",
            analyzer_set=["cheap"], quiet=True,
        )

    # autofixignore is ALWAYS constructed (load returns no-op when
    # .autofixignore is absent) — never None.
    assert captured["autofixignore"] is not None

    # All-default flags → byte-identity-safe Nones.
    assert captured["scoring_flags"] is None
    assert captured["class_aware_config"] is None


def test_class_aware_flag_constructs_class_aware_config(
    tmp_path: Path,
) -> None:
    """``crawler.expansion.class_aware = true`` → ``class_aware_config``
    is a ClassAwareConfig instance, not None.
    """
    from autofix.cli.cycle_runner import _run_crawl_once_body

    _make_repo(tmp_path, config={
        "version": 1, "mode": "preview", "budget": "cheap",
        "crawler": {"expansion": {"class_aware": True}},
    })

    captured = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return []

    with patch("autofix.crawl.picker.pick_next_batch", side_effect=_fake):
        _run_crawl_once_body(
            root=tmp_path, mode="preview", budget="cheap",
            analyzer_set=["cheap"], quiet=True,
        )

    assert isinstance(captured["class_aware_config"], ClassAwareConfig)
    assert captured["class_aware_config"].root == tmp_path


def test_scoring_flags_constructed_from_config(tmp_path: Path) -> None:
    """When any of the three scoring flags is True in
    .autofix/config.json, a ScoringFlags object is constructed and
    passed.
    """
    from autofix.cli.cycle_runner import _run_crawl_once_body

    _make_repo(tmp_path, config={
        "version": 1, "mode": "preview", "budget": "cheap",
        "crawler": {"scoring": {
            "entrypoint_boost": True,
            "low_value_class_penalty": False,
            "oversize_file_penalty": False,
        }},
    })

    captured = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return []

    with patch("autofix.crawl.picker.pick_next_batch", side_effect=_fake):
        _run_crawl_once_body(
            root=tmp_path, mode="preview", budget="cheap",
            analyzer_set=["cheap"], quiet=True,
        )

    sf = captured["scoring_flags"]
    assert isinstance(sf, ScoringFlags)
    assert sf.entrypoint_boost is True
    assert sf.low_value_class_penalty is False
    assert sf.oversize_file_penalty is False


def test_all_scoring_flags_off_yields_none_not_dataclass(
    tmp_path: Path,
) -> None:
    """Defensive: when every scoring flag is False, scoring_flags
    must be None (not a ScoringFlags(False, False, False)). The
    None form lets ``relevance`` short-circuit to the legacy
    formula without paying the classify_file + stat cost per
    candidate.
    """
    from autofix.cli.cycle_runner import _run_crawl_once_body

    _make_repo(tmp_path, config={
        "version": 1, "mode": "preview", "budget": "cheap",
        "crawler": {"scoring": {
            "entrypoint_boost": False,
            "low_value_class_penalty": False,
            "oversize_file_penalty": False,
        }},
    })

    captured = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return []

    with patch("autofix.crawl.picker.pick_next_batch", side_effect=_fake):
        _run_crawl_once_body(
            root=tmp_path, mode="preview", budget="cheap",
            analyzer_set=["cheap"], quiet=True,
        )

    assert captured["scoring_flags"] is None


def test_console_script_paths_threaded_through(tmp_path: Path) -> None:
    """``console_script_paths`` is threaded into pick_next_batch as a
    frozenset (possibly empty) — never None.
    """
    from autofix.cli.cycle_runner import _run_crawl_once_body

    _make_repo(tmp_path, config={"version": 1, "mode": "preview", "budget": "cheap"})

    captured = {}

    def _fake(**kwargs):
        captured.update(kwargs)
        return []

    with patch("autofix.crawl.picker.pick_next_batch", side_effect=_fake):
        _run_crawl_once_body(
            root=tmp_path, mode="preview", budget="cheap",
            analyzer_set=["cheap"], quiet=True,
        )

    csp = captured["console_script_paths"]
    assert isinstance(csp, frozenset)
