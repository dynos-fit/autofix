"""Integration tests for the crawl driver's stub wiring (ARCH-016 follow-up).

Exercises the real ``_analyze_bundle`` and
``_dispatch_repair_workflow`` paths now that the stubs are wired:

* ``_analyze_bundle`` invokes the funnel analyzer registry per file in
  the bundle.
* ``_dispatch_repair_workflow`` invokes
  :func:`autofix.cli.run_command._run_one_cycle` with the crawl's
  resolved analyzer set and a synthesized ``args`` namespace.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch



def _git_init_with_files(tmp_path: Path, files: list[str]) -> Path:
    """Initialize a tmp git repo with seed files."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.invalid"], cwd=tmp_path, check=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    for f in files:
        (tmp_path / f).write_text(f"# {f}\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_analyze_bundle_invokes_cheap_analyzer(tmp_path: Path) -> None:
    """Bundle with cheap analyzer → ``_analyze_bundle`` invokes the
    cheap callable from the funnel registry per file."""
    from autofix.crawl.driver import _analyze_bundle
    from autofix.crawl.bundles import Bundle

    src = tmp_path / "x.py"
    src.write_text("import os\n# os never used\n")
    file_paths = (src,)
    bundle = Bundle(
        seed_path=src,
        file_paths=file_paths,
        total_bytes=src.stat().st_size,
        fingerprint=Bundle.compute_fingerprint(file_paths),
    )

    cheap_findings = MagicMock(name="cheap_findings_iter")
    cheap_findings.__iter__ = lambda self: iter([MagicMock(rule_id="unused-import.intra-file")])

    fake_analyzer = MagicMock(return_value=cheap_findings)

    fake_registry = {"cheap": fake_analyzer}
    with patch.dict(
        "autofix.funnel.pipeline._ANALYZER_REGISTRY",
        fake_registry, clear=True,
    ):
        findings = _analyze_bundle(
            bundle=bundle, analyzer="cheap",
            root=tmp_path, commit_sha="abc",
        )

    fake_analyzer.assert_called_once()
    assert len(findings) == 1


def test_analyze_bundle_unknown_analyzer_returns_empty(tmp_path: Path) -> None:
    from autofix.crawl.driver import _analyze_bundle
    from autofix.crawl.bundles import Bundle

    src = tmp_path / "x.py"
    src.write_text("# noop\n")
    bundle = Bundle(
        seed_path=src, file_paths=(src,),
        total_bytes=src.stat().st_size,
        fingerprint=Bundle.compute_fingerprint((src,)),
    )
    findings = _analyze_bundle(
        bundle=bundle, analyzer="nonexistent",
        root=tmp_path, commit_sha="abc",
    )
    assert findings == []


def test_analyze_bundle_swallows_parse_failure_per_file(tmp_path: Path) -> None:
    """One bad file in a multi-file bundle doesn't abort the cycle —
    other files still contribute findings."""
    from autofix.crawl.driver import _analyze_bundle
    from autofix.crawl.bundles import Bundle

    good = tmp_path / "good.py"
    good.write_text("import os\n")
    missing = tmp_path / "missing.py"  # never written → parse_file fails

    bundle = Bundle(
        seed_path=good, file_paths=(good, missing),
        total_bytes=good.stat().st_size,
        fingerprint=Bundle.compute_fingerprint((good, missing)),
    )

    fake_analyzer = MagicMock(return_value=[MagicMock(rule_id="cheap")])
    with patch.dict(
        "autofix.funnel.pipeline._ANALYZER_REGISTRY",
        {"cheap": fake_analyzer}, clear=True,
    ):
        findings = _analyze_bundle(
            bundle=bundle, analyzer="cheap",
            root=tmp_path, commit_sha="abc",
        )

    # The good file contributed; the missing file was swallowed silently.
    assert fake_analyzer.call_count == 1
    assert len(findings) == 1


def test_dispatch_repair_workflow_invokes_run_one_cycle(tmp_path: Path) -> None:
    """Repair dispatch hands off to the existing ``_run_one_cycle``
    with the crawl's analyzer set + the right post-fix policy."""
    from autofix.crawl.driver import _dispatch_repair_workflow
    from autofix.crawl.bundles import Bundle

    _git_init_with_files(tmp_path, ["a.py"])
    bundle = Bundle(
        seed_path=tmp_path / "a.py",
        file_paths=(tmp_path / "a.py",),
        total_bytes=10,
        fingerprint=Bundle.compute_fingerprint((tmp_path / "a.py",)),
    )

    captured: dict = {}

    def _fake_one_cycle(args, analyzer_set, *, fresh_instance):
        captured["analyzer_set"] = analyzer_set
        captured["apply"] = args.apply
        captured["auto_llm"] = args.auto_llm
        captured["post_fix"] = args.post_fix
        return 0

    with patch(
        "autofix.cli.run_command._run_one_cycle", side_effect=_fake_one_cycle
    ):
        rc = _dispatch_repair_workflow(
            root=tmp_path, bundle=bundle, mode="pr",
            analyzers=["cheap", "llm:security"], quiet=True,
        )

    assert rc == 0
    assert captured["analyzer_set"] == ["cheap", "llm:security"]
    assert captured["apply"] is True
    assert captured["auto_llm"] is True  # llm:* in set → auto-llm
    assert captured["post_fix"] == "branch-pr"  # mode=pr


def test_dispatch_commit_mode_uses_branch_post_fix(tmp_path: Path) -> None:
    from autofix.crawl.driver import _dispatch_repair_workflow
    from autofix.crawl.bundles import Bundle

    _git_init_with_files(tmp_path, ["a.py"])
    bundle = Bundle(
        seed_path=tmp_path / "a.py",
        file_paths=(tmp_path / "a.py",),
        total_bytes=10,
        fingerprint=Bundle.compute_fingerprint((tmp_path / "a.py",)),
    )

    captured: dict = {}

    def _fake(args, _set, *, fresh_instance):
        captured["post_fix"] = args.post_fix
        return 0

    with patch("autofix.cli.run_command._run_one_cycle", side_effect=_fake):
        _dispatch_repair_workflow(
            root=tmp_path, bundle=bundle, mode="commit",
            analyzers=["cheap"], quiet=True,
        )
    assert captured["post_fix"] == "branch"


def test_dispatch_no_llm_in_analyzers_disables_auto_llm(tmp_path: Path) -> None:
    """No ``llm:*`` in analyzer set → ``--auto-llm`` is False."""
    from autofix.crawl.driver import _dispatch_repair_workflow
    from autofix.crawl.bundles import Bundle

    _git_init_with_files(tmp_path, ["a.py"])
    bundle = Bundle(
        seed_path=tmp_path / "a.py",
        file_paths=(tmp_path / "a.py",),
        total_bytes=10,
        fingerprint=Bundle.compute_fingerprint((tmp_path / "a.py",)),
    )

    captured: dict = {}
    def _fake(args, _set, *, fresh_instance):
        captured["auto_llm"] = args.auto_llm
        return 0

    with patch("autofix.cli.run_command._run_one_cycle", side_effect=_fake):
        _dispatch_repair_workflow(
            root=tmp_path, bundle=bundle, mode="pr",
            analyzers=["cheap", "linter:ruff"], quiet=True,
        )
    assert captured["auto_llm"] is False


def test_dispatch_swallows_run_one_cycle_exception(tmp_path: Path) -> None:
    """If ``_run_one_cycle`` raises, the dispatcher logs + returns 1
    so the crawl loop continues to the next bundle."""
    from autofix.crawl.driver import _dispatch_repair_workflow
    from autofix.crawl.bundles import Bundle

    _git_init_with_files(tmp_path, ["a.py"])
    bundle = Bundle(
        seed_path=tmp_path / "a.py",
        file_paths=(tmp_path / "a.py",),
        total_bytes=10,
        fingerprint=Bundle.compute_fingerprint((tmp_path / "a.py",)),
    )

    with patch(
        "autofix.cli.run_command._run_one_cycle",
        side_effect=RuntimeError("boom"),
    ):
        rc = _dispatch_repair_workflow(
            root=tmp_path, bundle=bundle, mode="pr",
            analyzers=["cheap"], quiet=True,
        )
    assert rc == 1
