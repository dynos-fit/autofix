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
    from autofix.cli.cycle_runner import _analyze_bundle
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
    from autofix.cli.cycle_runner import _analyze_bundle
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
    from autofix.cli.cycle_runner import _analyze_bundle
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


def _fake_findings(n: int = 1) -> list:
    """Build N MagicMock findings shaped like CandidateFinding."""
    return [
        MagicMock(
            rule_id="llm:security:secret-leak",
            path="a.py", start_line=10, end_line=10,
            finding_id=f"F-{i}",
        )
        for i in range(n)
    ]


def test_dispatch_invokes_fix_core_with_preloaded_findings(tmp_path: Path) -> None:
    """Repair dispatch passes the bundle's findings to ``_run_fix_core``
    via the preloaded findings path — no full-repo re-scan."""
    from autofix.cli.cycle_runner import _dispatch_repair_workflow
    from autofix.crawl.bundles import Bundle

    _git_init_with_files(tmp_path, ["a.py"])
    _ = Bundle(
        seed_path=tmp_path / "a.py",
        file_paths=(tmp_path / "a.py",),
        total_bytes=10,
        fingerprint=Bundle.compute_fingerprint((tmp_path / "a.py",)),
    )

    captured: dict = {}

    def _fake_fix_core(*, root, findings, apply_mode, suggest_mode,
                       auto_llm, force, max_llm_patches,
                       recovery_branch_already_captured, quiet):
        captured["findings"] = findings
        captured["apply_mode"] = apply_mode
        captured["auto_llm"] = auto_llm
        result = MagicMock()
        result.exit_code = 0
        result.applied_finding_ids = ["F-0"]
        return result

    findings = _fake_findings(1)

    with patch("autofix.cli.fix_command._run_fix_core",
               side_effect=_fake_fix_core), \
         patch("autofix.cli.post_fix_policy.apply_post_fix_policy",
               return_value="branch-pr") as mock_post:
        rc = _dispatch_repair_workflow(
            root=tmp_path, mode="pr",
            analyzers=["cheap", "llm:security"], quiet=True,
            findings=findings,
        )

    assert rc == 0
    assert captured["findings"] is findings  # preloaded, no rescan
    assert captured["apply_mode"] is True
    assert captured["auto_llm"] is True  # llm:* in set
    mock_post.assert_called_once()
    assert mock_post.call_args.kwargs["policy"] == "branch-pr"  # mode=pr


def test_dispatch_commit_mode_uses_branch_post_fix(tmp_path: Path) -> None:
    from autofix.cli.cycle_runner import _dispatch_repair_workflow
    from autofix.crawl.bundles import Bundle

    _git_init_with_files(tmp_path, ["a.py"])
    _ = Bundle(
        seed_path=tmp_path / "a.py",
        file_paths=(tmp_path / "a.py",),
        total_bytes=10,
        fingerprint=Bundle.compute_fingerprint((tmp_path / "a.py",)),
    )

    fake_result = MagicMock()
    fake_result.exit_code = 0
    fake_result.applied_finding_ids = ["F-0"]

    with patch("autofix.cli.fix_command._run_fix_core",
               return_value=fake_result), \
         patch("autofix.cli.post_fix_policy.apply_post_fix_policy",
               return_value="branch") as mock_post:
        _dispatch_repair_workflow(
            root=tmp_path, mode="commit",
            analyzers=["cheap"], quiet=True,
            findings=_fake_findings(1),
        )
    assert mock_post.call_args.kwargs["policy"] == "branch"


def test_dispatch_no_llm_in_analyzers_disables_auto_llm(tmp_path: Path) -> None:
    """No ``llm:*`` in analyzer set → ``auto_llm=False`` to ``_run_fix_core``."""
    from autofix.cli.cycle_runner import _dispatch_repair_workflow
    from autofix.crawl.bundles import Bundle

    _git_init_with_files(tmp_path, ["a.py"])
    _ = Bundle(
        seed_path=tmp_path / "a.py",
        file_paths=(tmp_path / "a.py",),
        total_bytes=10,
        fingerprint=Bundle.compute_fingerprint((tmp_path / "a.py",)),
    )

    captured: dict = {}

    def _fake_fix_core(**kwargs):
        captured["auto_llm"] = kwargs["auto_llm"]
        result = MagicMock()
        result.exit_code = 0
        result.applied_finding_ids = []  # nothing applied
        return result

    with patch("autofix.cli.fix_command._run_fix_core",
               side_effect=_fake_fix_core):
        _dispatch_repair_workflow(
            root=tmp_path, mode="pr",
            analyzers=["cheap", "linter:ruff"], quiet=True,
            findings=_fake_findings(1),
        )
    assert captured["auto_llm"] is False


def test_dispatch_swallows_fix_core_exception(tmp_path: Path) -> None:
    """If ``_run_fix_core`` raises, the dispatcher logs + returns 1
    so the crawl loop continues to the next bundle."""
    from autofix.cli.cycle_runner import _dispatch_repair_workflow
    from autofix.crawl.bundles import Bundle

    _git_init_with_files(tmp_path, ["a.py"])
    _ = Bundle(
        seed_path=tmp_path / "a.py",
        file_paths=(tmp_path / "a.py",),
        total_bytes=10,
        fingerprint=Bundle.compute_fingerprint((tmp_path / "a.py",)),
    )

    with patch("autofix.cli.fix_command._run_fix_core",
               side_effect=RuntimeError("boom")):
        rc = _dispatch_repair_workflow(
            root=tmp_path, mode="pr",
            analyzers=["cheap"], quiet=True,
            findings=_fake_findings(1),
        )
    assert rc == 1


def test_dispatch_skips_when_no_findings(tmp_path: Path) -> None:
    """Empty findings → no fix-core call, no post-fix call, return 0."""
    from autofix.cli.cycle_runner import _dispatch_repair_workflow
    from autofix.crawl.bundles import Bundle

    _git_init_with_files(tmp_path, ["a.py"])
    _ = Bundle(
        seed_path=tmp_path / "a.py",
        file_paths=(tmp_path / "a.py",),
        total_bytes=10,
        fingerprint=Bundle.compute_fingerprint((tmp_path / "a.py",)),
    )

    sentinel_fix = MagicMock()
    sentinel_post = MagicMock()
    with patch("autofix.cli.fix_command._run_fix_core",
               side_effect=sentinel_fix), \
         patch("autofix.cli.post_fix_policy.apply_post_fix_policy",
               side_effect=sentinel_post):
        rc = _dispatch_repair_workflow(
            root=tmp_path, mode="pr",
            analyzers=["cheap"], quiet=True,
            findings=[],
        )
    assert rc == 0
    sentinel_fix.assert_not_called()
    sentinel_post.assert_not_called()
