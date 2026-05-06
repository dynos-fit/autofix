# Rollback

## How to disable autofix-next

The `autofix-next` console script is registered as an entry point in `pyproject.toml` under `[project.scripts]`. To disable the new loop while preserving the legacy `autofix` CLI, remove or revert the line `autofix-next = "autofix_next.cli.main:main"` from `pyproject.toml`. This prevents the `autofix-next` command from being available without uninstalling the package. The legacy `autofix` entry point remains active and unaffected.

## State guaranteed untouched

The `autofix_next/migration.py` module is read-only on all legacy state surfaces. No writes occur to:

- `.autofix/state/**` — the legacy findings ledger and state cache
- `.autofix/autofix-policy.json` — the legacy policy file
- `.autofix/events.jsonl` — the legacy events log

These paths are locked by design. All reads of legacy state are delegated to the legacy module `autofix.state`, with zero mutations by `autofix_next`.

## Verification commands

After disabling `autofix-next`, verify that the legacy CLI state is unmodified by running these commands before and after an `autofix-next` run:

```bash
autofix list --root .
autofix policy --root .
```

Both commands must produce identical output before and after, since the legacy state was never mutated by the new loop. Any difference signals unexpected mutation.

## What is lost on rollback

The `autofix-next` loop accumulates internal cache state under `.autofix-next/state/**` (ClusterStore and related indices). This state is not consumed by the legacy `autofix` CLI, so disabling `autofix-next` forfeits that cache. However, no data that the legacy loop relied on is lost—the entire `.autofix/` tree remains intact and unmodified.
