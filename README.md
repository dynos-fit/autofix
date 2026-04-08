# autofix

Standalone repository scanner and repair runner.

`autofix` scans a target git repository, turns findings into structured actions, and uses the Dynos repair pipeline as its execution backend for safe fixes.

## Run

```bash
python3 -m autofix scan --root /path/to/target-repo
```

Or:

```bash
bin/autofix scan --root /path/to/target-repo
```

For a local wrapper install:

```bash
./install.sh
```

## Cron

Example hourly run:

```cron
0 * * * * cd /home/hassam/autofix-standalone && /home/hassam/autofix-standalone/bin/autofix scan --root /path/to/target-repo >> /var/log/autofix.log 2>&1
```

For safe debugging without opening real issues or PRs:

```cron
0 * * * * cd /home/hassam/autofix-standalone && /home/hassam/autofix-standalone/bin/autofix scan --root /path/to/target-repo --dry-run >> /var/log/autofix.log 2>&1
```

Top-level `.autofix/` keeps control files like `autofix-policy.json`, `events.jsonl`, `scan.lock`, `state/`, and `scans/`.
Aggregate latest-state files live under `.autofix/state/`. Per-scan history lives under `.autofix/scans/<scan-id>/`.

## Model

The workflow is:

1. scan the target repo
2. detect findings
3. route each finding by policy and risk
4. for safe findings, invoke the Dynos pipeline through shell commands
5. verify changes
6. open PRs or issues

## Requirements

- `python3`
- `git`
- `gh` for issues and PRs
- `claude` for autonomous fixes

## Operations

See [`docs/AUTOFIX_STANDALONE.md`](/home/hassam/autofix-standalone/docs/AUTOFIX_STANDALONE.md).
