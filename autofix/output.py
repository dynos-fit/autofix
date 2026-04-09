"""Human-readable and JSON formatting functions for CLI output.

Supports --json flag (acceptance criterion 21).
"""

from __future__ import annotations

import json
from typing import Any


def format_findings(findings: list[dict[str, Any]], *, as_json: bool = False) -> str:
    """Format a list of findings as JSON or human-readable text."""
    if as_json:
        return json.dumps(findings, indent=2)

    if not findings:
        return "No findings."

    lines: list[str] = []
    for f in findings:
        fid = f.get("finding_id", "unknown")
        severity = f.get("severity", "unknown")
        category = f.get("category", "")
        desc = f.get("description", "")
        confidence = f.get("confidence_score", "")
        evidence = f.get("evidence", {})
        file_path = evidence.get("file", "")
        line_num = evidence.get("line", "")

        header = f"[{severity.upper()}] {desc}"
        lines.append(header)
        lines.append(f"  id:         {fid}")
        lines.append(f"  category:   {category}")
        if confidence:
            lines.append(f"  confidence: {confidence}")
        if file_path:
            loc = f"  location:   {file_path}"
            if line_num:
                loc += f":{line_num}"
            lines.append(loc)
        lines.append("")

    return "\n".join(lines)


def format_repos(repos: list[dict[str, Any]], *, as_json: bool = False) -> str:
    """Format a list of repos as JSON or human-readable text."""
    if as_json:
        return json.dumps(repos, indent=2)

    if not repos:
        return "No repositories registered."

    lines: list[str] = []
    for entry in repos:
        path = entry.get("path", "")
        lines.append(path)

    return "\n".join(lines)


def format_config(config: dict[str, Any], *, as_json: bool = False) -> str:
    """Format a config dict as JSON or human-readable text."""
    if as_json:
        return json.dumps(config, indent=2, sort_keys=True)

    if not config:
        return "No configuration."

    lines: list[str] = []
    for key in sorted(config):
        lines.append(f"{key} = {config[key]}")

    return "\n".join(lines)


def format_scan_all_summary(summary: dict[str, Any], *, as_json: bool = False) -> str:
    """Format a scan-all summary as JSON or human-readable text."""
    if as_json:
        return json.dumps(summary, indent=2)

    total = summary.get("total", 0)
    succeeded = summary.get("succeeded", 0)
    failed = summary.get("failed", 0)
    skipped = summary.get("skipped", 0)

    lines: list[str] = [
        f"Scan-all summary: {total} total, {succeeded} succeeded, {failed} failed, {skipped} skipped",
    ]

    repos = summary.get("repos", [])
    for repo in repos:
        path = repo.get("path", "")
        status = repo.get("status", "")
        reason = repo.get("reason", "")
        line = f"  {path}: {status}"
        if reason:
            line += f" ({reason})"
        lines.append(line)

    return "\n".join(lines)


def format_benchmarks(benchmarks: dict[str, Any], *, as_json: bool = False) -> str:
    """Format benchmark data as JSON or human-readable text."""
    if as_json:
        return json.dumps(benchmarks, indent=2)

    if not benchmarks:
        return "No benchmark data."

    lines: list[str] = []
    for key in sorted(benchmarks):
        lines.append(f"{key}: {benchmarks[key]}")

    return "\n".join(lines)


def format_suppressions(suppressions: list[dict[str, Any]], *, as_json: bool = False) -> str:
    """Format a list of suppressions as JSON or human-readable text."""
    if as_json:
        return json.dumps(suppressions, indent=2)

    if not suppressions:
        return "No active suppressions."

    lines: list[str] = []
    for s in suppressions:
        parts: list[str] = []
        if s.get("finding_id"):
            parts.append(f"finding_id={s['finding_id']}")
        if s.get("category"):
            parts.append(f"category={s['category']}")
        if s.get("path_prefix"):
            parts.append(f"path_prefix={s['path_prefix']}")
        if s.get("until"):
            parts.append(f"until={s['until']}")
        if s.get("reason"):
            parts.append(f"reason={s['reason']}")
        lines.append("  ".join(parts))

    return "\n".join(lines)
