#!/usr/bin/env python3
"""Reconstruct the final LLM review prompt for a target repo."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from autofix.llm_io import build_review_prompt, build_review_prompt_for_file
from autofix.state import load_findings


def latest_scan_dir(root: Path) -> Path:
    scans_root = root / ".autofix" / "scans"
    scan_dirs = sorted([path for path in scans_root.iterdir() if path.is_dir()])
    if not scan_dirs:
        raise FileNotFoundError(f"No scan folders found under {scans_root}")
    return scan_dirs[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Show the exact LLM prompt for the latest autofix scan")
    parser.add_argument("--root", required=True, help="Target repo root")
    parser.add_argument("--scan-dir", default=None, help="Optional explicit scan directory")
    parser.add_argument("--out", default=None, help="Optional path to write the prompt instead of stdout")
    parser.add_argument("--out-dir", default=None, help="Optional directory to write one prompt per selected file")
    parser.add_argument("--single-file", default=None, help="Optional single review file path")
    parser.add_argument("--trim-lines", type=int, default=None, help="Optional per-file line trim for easier viewing")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    scan_dir = Path(args.scan_dir).resolve() if args.scan_dir else latest_scan_dir(root)
    selected_path = scan_dir / "selected-files.json"
    selected = json.loads(selected_path.read_text())
    findings = load_findings(root)
    file_truncation = args.trim_lines or 400

    if args.out_dir:
        out_dir = Path(args.out_dir).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        for rel in selected.get("review_files", []):
            prompt = build_review_prompt_for_file(
                root,
                selected_files=selected.get("selected_files", []),
                review_file=rel,
                findings_list=findings,
                file_truncation=file_truncation,
            )
            safe_name = rel.replace("/", "__")
            (out_dir / f"{safe_name}.prompt.txt").write_text(prompt)
        print(out_dir)
        return 0

    if args.single_file:
        prompt = build_review_prompt_for_file(
            root,
            selected_files=selected.get("selected_files", []),
            review_file=args.single_file,
            findings_list=findings,
            file_truncation=file_truncation,
        )
    else:
        prompt = build_review_prompt(
            root,
            selected_files=selected.get("selected_files", []),
            review_files=selected.get("review_files", []),
            findings_list=findings,
            file_truncation=file_truncation,
        )

    if args.out:
        out_path = Path(args.out).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(prompt)
        print(out_path)
        return 0

    print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
