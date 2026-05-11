# Getting started

`autofix` finds bugs in your code and (optionally) fixes them. Three
commands — that's the whole UX.

```bash
$ autofix init       # one-time: pick mode + budget
$ autofix start      # daemonize: runs continuously in the background
$ autofix status     # peek at what it's doing right now
```

Plus `autofix logs` to tail the daemon log and `autofix stop` to halt
it.

## Install

```bash
git clone https://github.com/dynos-fit/autofix
cd autofix
./install.sh
```

You need:

- Python 3.11–3.13.
- `git` (autofix reads `git diff` to scope its scans).
- `claude` CLI on your PATH (or any other LLM backend autofix can
  reach). Without an LLM, autofix still runs end-to-end — it just
  doesn't generate explanations or LLM patches.
- `gh` CLI (only if you want autofix to open PRs).

## First-time setup

In your repo:

```bash
$ autofix init
What should autofix do?
  [1] Find bugs and open PRs for fixes (recommended)
  [2] Find bugs but never modify code
  [3] Find bugs and commit fixes directly to current branch
Choice (default: 1):

How much should it spend per day?
  [1] Cheap        (~$0.50/day, 1 bundle/cycle, 60-min interval)
  [2] Balanced     (~$2/day,    5 bundles/cycle, 30-min interval) (recommended)
  [3] Aggressive   (~$10/day,   20 bundles/cycle, 5-min interval)
Choice (default: 2):

Repository root (default: /your/repo):
✓ wrote /your/repo/.autofix/config.json
```

Three questions, sane defaults. Press enter on each and you get
PR mode + balanced budget — that's what most people want.

## Run it

```bash
$ autofix start
autofix: daemon started (PID 12345)
$ autofix logs
autofix: cycle picked 10 (bundle, analyzer) pairs
... (tails the daemon log; the daemon wakes on the configured interval)
```

`autofix start` reads `.autofix/config.json` and runs the crawl as a
detached background process. Run `autofix stop` to halt it.

If you want to validate the loop before letting it run autonomously,
use the foreground forms (which require `--root`):

```bash
$ autofix --root . --once     # one cycle in the foreground, then exit
$ autofix --root .            # continuous crawl in the foreground (Ctrl-C to stop)
```

If you set up `init` with `preview` mode but want to apply this
once (foreground form):

```bash
$ autofix --root . --apply    # overrides config preview → commit for this run
```

## Check on it

```bash
$ autofix status
running: PID 12345
ledger:  47 / 152 files seen, 3 findings recorded in last 24h
recent:  abc12345 (2m ago) — llm:security on autofix/cli/run_command.py
next:    cycle pending (driver running)
```

Tells you the daemon's PID, how much of the repo it's covered, the
most recent cycle, and whether more is coming.

## What it actually does, step by step

Every cycle, autofix:

1. **Picks bundles.** A bundle is a file plus its 1-hop neighbors
   in the dependency graph (caller, callee, sibling subclass).
   Bundles are bounded by 5 files / 50KB / 1 hop — whichever
   trips first wins.

2. **Scores them.** `priority = freshness × relevance`. Files that
   changed since their last scan jump to the front. Files that
   churn often + are imported by many other files rank high.

3. **Runs analyzers** on the top-K bundles. Cheap analyzer
   (unused-import, free) plus 2-4 LLM bug-finders depending on
   your budget.

4. **Caches everything.** Same prompt + same commit SHA + same
   model = cache hit, free. Re-running a clean cycle costs $0.

5. **If findings + mode != preview**, opens a fix PR (or commits
   directly).

After ~24-48 cycles your ledger has covered the whole repo.
Subsequent cycles are mostly cache hits — the daemon settles into
re-scanning hot files only and your bill stabilizes.

## Cost shape

| Mode | Per cycle | Per day | What you get |
|---|---|---|---|
| Cheap | 1 bundle × 2 analyzers = 2 LLM calls | ~$0.50 | Every file scanned every ~3 days; security-only LLM judgment |
| Balanced | 5 bundles × 3 analyzers = 15 LLM calls | ~$2 | Every file in ~24h; security + code-quality LLM judgment |
| Aggressive | 20 bundles × 5 analyzers = 100 LLM calls | ~$10 | Every file in ~6h; full LLM bug-finder set |

The numbers above are upper bounds. In practice ~80% of cycles are
mostly cache hits (most files don't change between cycles), so
real spend is 30-50% of the worst case.

## Power-user reference

The default surface is intentionally tiny. If you want fine
control:

- `autofix --help-advanced` — full subcommand + flag reference
- `autofix scan/run/fix/watch` — one-shot or interactive primitives
- `docs/crawling.md` — the architecture, the scoring math, and
  every tunable knob
- `docs/crawling-tuning.md` — when to enable each optional flag
  (entrypoint boost, low-value-class penalty, class-aware
  expansion, impact-cone mode) and how to read the debug output
- `.autofix/config.json` — the full set of operator-facing
  configuration keys (including the optional `crawler.*` flags)
- `.autofixignore` — drop a `.gitignore`-style file at the repo
  root to further-exclude paths from the crawl
- `autofix --debug-crawl --once` — emit per-cycle stats (top
  seeds, score breakdowns, bundle stats) to stderr; useful when
  tuning

But for the quick-start path you don't need any of that. `init`,
`autofix start`, `autofix status`. Done.

## Troubleshooting

**"It says `not running` in status."** — `autofix` exited (Ctrl-C,
crash, system reboot). Run `autofix` again to start a new daemon.

**"It found a bug I don't want fixed."** — Add the file or rule to
the suppression list in `.autofix/autofix-policy.json`, or push
back on the PR.

**"It's burning too much money."** — Re-run `autofix init` and pick
`cheap`. Or stop the daemon (Ctrl-C) and run periodically via cron
instead (see "Power-user reference" above).

**"I don't see any findings."** — Either your code is clean (yay)
or autofix hasn't picked any of your hot files yet. Check
`autofix status` for the ledger count; let it run a few hours.
