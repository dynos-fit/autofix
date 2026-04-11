#!/usr/bin/env python3
"""Run a Python module or script under benchmark tracing patches."""

from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path

from benchmarks.agent_efficiency.instrumentation.core import apply_patch_specs, load_patch_specs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-file", required=True)
    parser.add_argument("--patch-config", required=True)
    parser.add_argument("--module")
    parser.add_argument("--script")
    parser.add_argument("args", nargs="*")
    ns = parser.parse_args()

    if not ns.module and not ns.script:
        raise SystemExit("one of --module or --script is required")

    specs = load_patch_specs(ns.patch_config)
    handles = apply_patch_specs(specs, trace_file=ns.trace_file)
    try:
        target_args = [ns.module or ns.script, *ns.args]
        sys.argv = target_args
        if ns.module:
            runpy.run_module(ns.module, run_name="__main__")
        else:
            script_path = str(Path(ns.script).resolve())
            runpy.run_path(script_path, run_name="__main__")
    finally:
        for handle in reversed(handles):
            handle.restore()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
