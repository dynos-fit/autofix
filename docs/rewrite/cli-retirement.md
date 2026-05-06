# CLI retirement

This document covers the cutover from the legacy `autofix` CLI to the
clean-slate `autofix-next` CLI. It is the operator-facing companion to
`docs/rewrite/roadmap.md` and pairs with the rollback procedure in
`docs/rewrite/rollback.md`.

## Deprecation map

Every legacy `autofix` subcommand is mapped to one of three actions:
`replace` (a new `autofix-next` command does the equivalent thing),
`rename` (the same behavior under a new name), or `remove` (the verb is
retired with no direct successor; orchestration moves elsewhere).

| legacy_command | action | new_command | notes |
|---|---|---|---|
| `autofix scan` | replace | `autofix-next scan` | `--root` flag removed; the new CLI infers the repo root from cwd and from the ScanEvent. |
| `autofix list` | replace | `autofix-next show finding --json` | Future successor command. List semantics preserved through the same finding-inspection verb. |
| `autofix clear` | remove | null | Replaced by `autofix-next index vacuum` (planned successor) plus a policy-driven retention window. There is no first-class `clear` verb. |
| `autofix policy` | rename | `autofix-next policy --show` | Behavior unchanged; the policy file (`.autofix/autofix-policy.json`) shape is preserved byte-for-byte. |
| `autofix sync-outcomes` | replace | `autofix-next watch` | Outcomes sync is now event-driven via the watcher. The explicit subcommand is retired. |
| `autofix benchmark` | remove | null | Benchmark summaries move to `benchmarks/agent_bench/` harness outputs; the core CLI no longer reports benchmark metrics directly. |
| `autofix suppress` | rename | `autofix-next policy --edit suppression` | Planned successor initiative. Until that lands, suppressions remain a file-edit operation on `.autofix/autofix-policy.json`. |
| `autofix init` | replace | `autofix-next doctor --init` | Future successor command for guided setup; a documented manual policy file template is also provided. |
| `autofix daemon` | replace | `autofix-next watch` | Watchman-backed watcher; the Watchman `is_fresh_instance` signal flows into the change detector. |
| `autofix repo` | remove | null | Multi-repo management moves out of the core CLI; orchestration lives in cron, CI matrix, or an external `autofix-next-orchestrator`. |
| `autofix config` | rename | `autofix-next policy --show` | No separate config verb; `.autofix/autofix-policy.json` is the single source of truth. |
| `autofix scan-all` | replace | external orchestration | The built-in multi-repo sweep is retired in favor of per-repo `autofix-next scan` invocations from CI/cron. |

## Retirement calendar

Three relative milestones, anchored to the cutover release date
(operators substitute their own `T+0` for the calendar to be
actionable):

- **T+0** — `autofix-next` ships its complete CLI surface. The legacy
  `autofix <subcommand>` continues to function unchanged, but now emits
  a one-line deprecation banner to stderr on each invocation. (Banner
  code itself ships in a separate cutover release commit, NOT in the
  task that produced this document.)

- **T+30 days** — Legacy `autofix <subcommand>` exits non-zero unless
  `AUTOFIX_LEGACY=1` is set in the environment. Operators with cron
  entries or CI pipelines pinning the legacy CLI must either migrate
  their commands by this date or set the environment variable to
  buy a 60-day grace window.

- **T+90 days** — The legacy `autofix` console-script entry-point is
  removed from `pyproject.toml` `[project.scripts]`. The `autofix.cli`
  module remains in-tree (the code is not deleted) but is no longer
  hooked to a binary. After this milestone, `autofix scan --root .`
  fails with `command not found` regardless of `AUTOFIX_LEGACY`.

## Operator migration steps

The most common legacy invocation is `autofix scan --root .` running
in cron or a CI pipeline. The new equivalent:

| Legacy | New |
|---|---|
| `autofix scan --root .` | `autofix-next scan` |
| `autofix scan --root /path/to/repo` | `cd /path/to/repo && autofix-next scan` |

The `--root` flag is gone — `autofix-next scan` infers the repo root
from the current working directory. CI scripts that change directory
before invoking the scanner need no other change.

### FAQ

**Q: I run `autofix daemon start` from systemd. What replaces it?**
Use `autofix-next watch --root /path/to/repo`. The watcher is
Watchman-backed; install the optional `watch` extra:

```sh
pip install autofix-standalone[watch]
```

You also need the `watchman` daemon binary on the host (Homebrew:
`brew install watchman`; Debian/Ubuntu: `apt install watchman`).

**Q: I run `autofix policy` to see the current policy. What replaces it?**
`autofix-next policy --show` prints the policy as sorted-key JSON. To
type-check the policy file structurally, use `autofix-next policy
--validate` (exits 2 with one diagnostic line per issue).

**Q: I edit `autofix-policy.json` via `autofix suppress add ...`. What
do I do now?**
Edit `.autofix/autofix-policy.json` directly with a text editor.
A future `autofix-next policy --edit suppression` command is planned
but does not ship in the current cutover.

**Q: I rely on `autofix scan-all` to sweep multiple repos. What do I
do?**
Loop over your repos in cron / CI, invoking `autofix-next scan` per
repo. Multi-repo orchestration is no longer a built-in concern of the
core CLI; the new `autofix-next` is single-repo-per-invocation by
design.

**Q: What about `autofix benchmark`?**
Benchmark outputs now live under `benchmarks/agent_bench/`. The harness
contract (`benchmarks/agent_bench/autofix_adapter.py::build_agent`) is
preserved byte-identically so existing agent-bench fixtures keep
running against either loop.

**Q: What about `autofix init` for a fresh repo?**
The current cutover ships no automated init. Create
`.autofix/autofix-policy.json` manually (a template lives in
`docs/rewrite/`) and run `autofix-next scan` to produce the first
events row. A future `autofix-next doctor --init` will guide this.
