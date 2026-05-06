# Changelog

## Unreleased — scorched-earth cleanup (task-20260506-003)

Single-package consolidation. Renames `autofix_next` → `autofix` and
deletes the legacy CLI surface entirely.

### Removed

- Legacy `autofix/` modules: `app.py`, `cli.py` (legacy argparse),
  `scanner.py`, `detectors.py`, `crawler.py`, `daemon.py`, `config.py`,
  `init.py`, `repo.py`, `scan_all.py`, `output.py`, `routing.py`,
  `state.py`, `backend.py`, `runtime/`, `__main__.py`.
- Legacy CLI verbs: `autofix list`, `autofix clear`, `autofix
  sync-outcomes`, `autofix benchmark`, `autofix suppress`,
  `autofix init`, `autofix daemon`, `autofix repo`, `autofix config`,
  `autofix scan-all`. The `autofix scan` and `autofix policy` verbs
  are preserved by the new CLI; the others are retired.
- `autofix-next` console script. Replaced by a single `autofix`
  entry resolving to `autofix.cli.main:main`.
- 13 legacy test files at `tests/test_*.py` (~2,300 LOC).
- `demo_llm_prompt.py` development artifact.
- `docs/rewrite/` directory (rewrite-era roadmap, retirement
  calendar, target-architecture, rollback, etc.).

### Changed

- Package namespace: every `autofix_next.X` import is now `autofix.X`.
- Console script: `autofix` resolves to the new loop's
  `autofix.cli.main:main`.
- On-disk scan output: `.autofix-next/scans/<scan-id>/` is now
  `.autofix/scans-next/<scan-id>/`.
- On-disk SCIP index: `.autofix-next/state/index/` is now
  `.autofix/state/index/`.
- On-disk embedding sidecar: `.autofix-next/state/embedding-sidecar/`
  is now `.autofix/state/embedding-sidecar/`.
- The locked seams (`agent_loop.py`, `llm_backend.py`, `llm_io/`)
  are relocated into `autofix/` at the top level. The benchmark
  adapter at `benchmarks/agent_bench/autofix_adapter.py` was
  updated to import from `autofix.agent_loop` and
  `autofix.llm_backend` (this deliberately retires the rewrite-era
  locked-surface contract, which was process discipline for the
  in-flight rewrite, not a permanent invariant).
- `migration.py` no longer depends on `autofix.state`. It reads
  `<root>/.autofix/state/current/findings.json` via stdlib JSON.

### Operator migration

Operators with cron entries pointing at `autofix-next scan` or
`.autofix-next/scans/...` MUST update:

- `autofix-next scan --root .` → `autofix scan --root .`
- `.autofix-next/scans/<scan-id>/findings.sarif` →
  `.autofix/scans-next/<scan-id>/findings.sarif`
- Any leftover `.autofix-next/` directory in a developer working
  tree is a runtime artifact; safe to `rm -rf` after this release.
- `AUTOFIX_NEXT_OFFLINE` env var → `AUTOFIX_OFFLINE`
- `AUTOFIX_NEXT_BIN_CACHE` env var → `AUTOFIX_BIN_CACHE`
- `AUTOFIX_NEXT_WATCH_ONCE` env var → `AUTOFIX_WATCH_ONCE`

The 4 retired CLI verbs have no `autofix` successor; operators read
findings via SARIF and edit policy via direct file edit on
`.autofix/autofix-policy.json`.
