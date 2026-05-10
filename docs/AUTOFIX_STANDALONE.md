# Standalone Autofix

`autofix` is a standalone scanner that runs from cron (or any other
scheduler) and emits findings as SARIF + JSONL telemetry. Repair is
delegated to the same `autofix` CLI: `autofix fix --apply` for the
deterministic tier and `autofix run --apply --auto-llm` for the
LLM-patch tier.

## Run

From a venv created by `./install.sh`:

```bash
.venv/bin/autofix scan --root /path/to/target-repo
```

Or, with `autofix` on `$PATH`:

```bash
autofix scan --root /path/to/target-repo
```

The console entry point is declared in `pyproject.toml`
(`[project.scripts] autofix = "autofix.cli.main:main"`); `./install.sh`
sets it up inside the chosen venv.

The target repo should have:

- `git`
- `gh` configured for the target repository (only required for
  PR-creating runs)
- `claude` installed if you want automatic LLM-backed fixes

If `claude` is unavailable, scans still run end-to-end — only the
LLM-patch tier of repair is unavailable.

## Cron

Example: run a scan every hour and append logs.

```cron
0 * * * * cd /path/to/autofix-standalone && /path/to/autofix-standalone/.venv/bin/autofix scan --root /path/to/target-repo >> /var/log/autofix.log 2>&1
```

Example: run every 15 minutes with explicit runtime directories for the
target repo.

```cron
*/15 * * * * cd /path/to/autofix-standalone && AUTOFIX_RUNTIME_DIR=/path/to/target-repo/.autofix AUTOFIX_PERSISTENT_DIR=/path/to/target-repo/.autofix /path/to/autofix-standalone/.venv/bin/autofix scan --root /path/to/target-repo >> /var/log/autofix.log 2>&1
```

For a long-running daemon (instead of cron), use `autofix start` —
it daemonizes `autofix --root <p>` via `subprocess.Popen` with
`start_new_session=True` and writes a pidfile at `.autofix/crawl.pid`.

## Behavior

When the scan fires:

1. `autofix` derives a git-diff-scoped changeset (default
   `HEAD~1..HEAD`; override with `--diff-range` or use `--full-sweep`).
2. The funnel parses + scores + dedups + ranks findings.
3. SARIF is written to `<root>/.autofix/scans/<scan-id>/findings.sarif`.
4. Envelope rows are appended to `.autofix/events.jsonl`.
5. Findings are kept on disk; repair is a separate explicit pass via
   `autofix fix --apply` (deterministic deletions) or
   `autofix run --apply --auto-llm` (deterministic + LLM patches with
   verify and retry).

## Storage

`autofix/platform.py::runtime_state_dir(root)` resolves to (in order):

- the value of `AUTOFIX_RUNTIME_DIR` if set
- `<root>/.autofix/` if it exists
- otherwise `<root>/.dynos/` if it exists
- otherwise `<root>/.autofix/` (the default)

You can override with:

- `AUTOFIX_RUNTIME_DIR`
- `AUTOFIX_PERSISTENT_DIR`

Top-level `.autofix/` keeps control files and indexes, for example:

- `autofix-policy.json`
- `events.jsonl`
- `scan.lock`
- `state/`
- `scans/`

Current aggregate state lives under:

- `.autofix/state/current/findings.json`
- `.autofix/state/current/scan-coverage.json`
- `.autofix/state/current/metrics.json`
- `.autofix/state/current/benchmarks.json`

Historical state snapshots live under:

- `.autofix/state/history/<scan-id>/`

Historical per-scan artifacts live under:

- `.autofix/scans/<scan-id>/`
