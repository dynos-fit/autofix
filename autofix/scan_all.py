"""Scan all registered repositories sequentially.

Implements ``autofix scan-all`` (acceptance criterion 20).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from autofix.repo import _load_repos, _repos_file
from autofix.scanner import run_scan_with_lock


@dataclass
class ScanAllResult:
    """Result of a scan-all run."""

    exit_code: int = 0
    output: str = ""


def run_scan(root: Path, **kwargs: object) -> int:
    """Run a scan in a single repository.

    This is the real implementation invoked during production runs.
    Tests patch this function to avoid heavy scanner dependencies.
    """
    try:
        from autofix.app import runtime_factory

        max_findings = int(kwargs.get("max_findings", 0)) if kwargs.get("max_findings") else 0
        return run_scan_with_lock(root.resolve(), max_findings=max_findings, runtime=runtime_factory(root=root.resolve()))
    except RuntimeError:
        return 1
    except Exception:
        return 1


def cmd_scan_all(*, home_dir: Path) -> ScanAllResult:
    """Iterate every repo in ``~/.autofix/repos.json`` and scan sequentially.

    * Skips repos whose path no longer exists on disk (with a warning).
    * Continues scanning remaining repos even if one fails.
    * Returns non-zero exit code if any individual scan failed.
    """
    repos_path = _repos_file(home_dir)
    repos = _load_repos(repos_path)

    if not repos:
        return ScanAllResult(exit_code=0, output="No repos registered.")

    lines: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []

    for entry in repos:
        repo_path_str = entry.get("path", "")
        if not repo_path_str:
            continue

        repo_path = Path(repo_path_str)

        if not repo_path.exists():
            skipped.append(repo_path_str)
            lines.append(f"WARNING: skip {repo_path_str} (path does not exist)")
            continue

        try:
            exit_code = run_scan(repo_path)
        except Exception as exc:
            exit_code = 1
            lines.append(f"ERROR: scan of {repo_path_str} raised: {exc}")

        if exit_code != 0:
            failed.append(repo_path_str)
            lines.append(f"FAIL: {repo_path_str} (exit code {exit_code})")
        else:
            lines.append(f"OK: {repo_path_str}")

    # Summary
    total = len(repos)
    ok_count = total - len(failed) - len(skipped)
    lines.append(
        f"\nSummary: {ok_count} passed, {len(failed)} failed, {len(skipped)} skipped "
        f"out of {total} repo(s)."
    )

    overall_exit = 1 if failed else 0
    return ScanAllResult(exit_code=overall_exit, output="\n".join(lines))
