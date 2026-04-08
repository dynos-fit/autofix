# Standalone Autofix

`autofix` is a standalone scanner that can run from cron and use the Dynos pipeline as its repair engine.

## Run

From the repo root:

```bash
python3 -m autofix scan --root /path/to/target-repo
```

Or through the wrapper:

```bash
bin/autofix scan --root /path/to/target-repo
```

To install a user-local wrapper:

```bash
./install.sh
```

The target repo should have:

- `git`
- `gh` configured for the target repository
- `claude` installed if you want automatic fixes

If `claude` is unavailable, scans can still run, but automatic code fixes will fail closed.

For debugger-friendly scans without side effects:

```bash
python3 -m autofix scan --root /path/to/target-repo --dry-run
```

`--dry-run` still scans, deduplicates, classifies, and routes findings, but it does not open real issues or PRs.

## Cron

Example: run every hour and append logs.

```cron
0 * * * * cd /home/hassam/autofix-standalone && /home/hassam/autofix-standalone/bin/autofix scan --root /path/to/target-repo >> /var/log/autofix.log 2>&1
```

Example: run every 15 minutes with explicit runtime directories for the target repo.

```cron
*/15 * * * * cd /home/hassam/autofix-standalone && AUTOFIX_RUNTIME_DIR=/path/to/target-repo/.autofix AUTOFIX_PERSISTENT_DIR=/path/to/target-repo/.autofix /home/hassam/autofix-standalone/bin/autofix scan --root /path/to/target-repo >> /var/log/autofix.log 2>&1
```

## Behavior

When cron fires:

1. `autofix` scans the target codebase.
2. Safe findings are routed to the Dynos execution backend.
3. The backend invokes the Dynos pipeline through shell commands, including `/dynos-work:start`.
4. Verified changes are pushed and opened as PRs.
5. Higher-risk or non-fixable findings open issues instead.

## Storage

By default `autofix` uses:

- target repo `.autofix/` if it exists
- otherwise target repo `.dynos/` if it exists
- otherwise target repo `.autofix/`

You can override that with:

- `AUTOFIX_RUNTIME_DIR`
- `AUTOFIX_PERSISTENT_DIR`
