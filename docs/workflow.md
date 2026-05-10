# The autofix run loop

This doc explains what happens between `autofix run` and the exit
code: how the workflow state machine sequences the phases, what the
verify state actually checks, how retries work, what
recovery branches look like on disk, and how the post-fix policy
opens (or doesn't open) a PR.

If you only want to use `autofix run`, read the
[README](../README.md). This doc is for operators tuning the loop
or debugging a stuck run.

## States

Nine states. Eight are non-terminal; three are terminal
(`DONE`, `FAILED`, `HUMAN_REVIEW`).

| State | What happens |
|---|---|
| `SCANNING` | Diff-scoped scan via `_run_scan_core` (full-sweep when `--watch` reports `is_fresh_instance=True`). Findings are deduped by `(path, start_line, end_line)`. |
| `TRIAGING` | `coordinate_repairs(findings)` partitions findings into fix tiers: deterministic deletion, LLM patch, or human review. |
| `PLANNING` | For LLM-tier findings, `produce_patch(task)` generates a unified diff via the LLM and validates it with `git apply --check --no-unsafe-paths`. |
| `APPLYING` | (`--apply` only) Runs `_run_fix_core` to delete safe lines and `git apply` the LLM patches. Recovery branch is captured here on the first iteration of `--auto-llm`. |
| `VERIFYING` | Runs the project's tests via `subprocess.run` with timeout, then re-scans to confirm applied finding-ids are gone and no new findings appeared. |
| `DONE` | Terminal: every applied finding is gone from the post-scan, and tests pass (or there's no test runner). Exit code `0`. |
| `RETRY` | Some applied finding-ids are still present in the post-scan. Re-coordinates the fresh post-scan findings and re-enters `TRIAGING`. Up to `--max-retries` (default 3) iterations. |
| `HUMAN_REVIEW` | Terminal: preview-only mode (no `--apply`). Exit code `3`. |
| `FAILED` | Terminal: scan failure, max-retries exhausted, or `git apply` failure. Exit code `1`. |

Every transition writes one row to
`<root>/.autofix/runs/<run-id>/state.jsonl`. Rows are append-only
JSONL: `{ts, run_id, from_state, to_state, evidence_sha256, reason,
attempt, event_id}`.

## Retry semantics

A retry fires when the VERIFYING re-scan finds at least one of the
`applied_finding_ids` still present. The loop:

1. Re-scans the working tree (post-`git apply`).
2. Computes `unresolved = applied_finding_ids ∩ post_finding_ids`.
3. If `unresolved == ∅` → `DONE`.
4. Otherwise → `RETRY` → re-coordinate post-scan findings → re-enter
   `TRIAGING`.

`--max-retries` (default 3) caps the number of retry iterations per
`run` invocation. After exhaustion, the workflow ends in `FAILED`
with `reason="max_retries_exhausted"`.

## VERIFYING — what gets verified

The verify primitive (`autofix/workflow/verify.py::run_verification`)
returns a `VerifyResult` NamedTuple:

```python
class VerifyResult(NamedTuple):
    test_passed: bool | None       # None = no runner detected
    test_runner: str | None        # "pytest" | "jest" | "go-test" | "configured"
    test_command: tuple[str, ...] | None
    test_log_path: Path | None     # .autofix/runs/<run-id>/verify-<attempt>.log
    new_finding_count: int         # post_set − pre_set
    unresolved_finding_ids: frozenset[str]  # applied & post
    post_finding_ids: tuple[str, ...]
    regressed: bool                # any of the above signals failure
```

Test runner is auto-detected by walking these markers in priority order:

1. `pyproject.toml` → `pytest -x`
2. `setup.py` → `pytest -x`
3. `setup.cfg` → `pytest -x`
4. `package.json` → `npm test`
5. `go.mod` → `go test ./...`

`.autofix/config.json::test.command` overrides detection. When no
marker matches and no override is set, `test_passed` is `None` and
the test signal is dropped — only the re-scan determines DONE/RETRY.

Test failure modes are mapped:

| Signal | `test_passed` |
|---|---|
| Exit code `0` | `True` |
| Non-zero exit | `False` |
| `FileNotFoundError` (runner missing) | `False` |
| `subprocess.TimeoutExpired` | `False` |
| OSError (filesystem error) | raises `VerifyScanFailed` (the run goes FAILED, not RETRY) |
| No marker, no override | `None` |

## Recovery branches

When `--auto-llm` is set, autofix captures a recovery branch
**once per `run` invocation** at the pre-run HEAD:

```
autofix/pre-fix-snapshot-<utc>
```

The branch name uses `%Y%m%dT%H%M%SZ` (no separators in the time
part — git rejects colons in ref names). Captured BEFORE any
`APPLYING` state entry. To rewind:

```bash
git checkout autofix/pre-fix-snapshot-20260508T012345Z
```

A new recovery branch is captured per `autofix run` invocation,
before the first APPLYING transition. Watch sessions reuse the
session's branch across cycles.

## Post-fix policy

After `DONE` with a non-empty `applied_finding_ids` set, the
post-fix policy module
(`autofix/cli/post_fix_policy.py::apply_post_fix_policy`) optionally
creates a fresh branch and commits the applied changes. Three values:

### `working-tree` (default)

No-op. Working tree retains the applied changes; you stage and
commit yourself. Identical to the pre-ARCH-015 behavior.

### `branch`

```
git rev-parse --abbrev-ref HEAD                  # capture original_branch
git checkout -b autofix/fixes-<run-id>           # create branch
git add -A                                        # stage everything
git commit -m "<title>" -m "<body>"               # see commit-message format below
git checkout <original_branch>                    # restore
```

Commit message:

```
autofix: applied <N> fixes (run <run-id>)

- finding-id: <fid-1>
- finding-id: <fid-2>
...
```

Bullets are sorted ascending. The branch persists; cleanup is up
to you.

### `branch-pr`

Same as `branch`, plus:

```
gh pr create --fill --base <original_branch> --head autofix/fixes-<run-id>
```

`gh` reads the commit message and uses it as the PR body. If `gh`
is missing (`shutil.which("gh") is None`), degrades to `branch`
with a stderr warning — never fails the run.

### Graceful degradation

Any subprocess error mid-policy is caught:

1. Logs `autofix: post-fix policy <p> failed: <exc>; reverting to working-tree` to stderr.
2. Best-effort restores `original_branch` via `git checkout <original>` (errors swallowed).
3. Returns `working-tree` (the policy effectively reverted to safe default).
4. Does NOT raise — the run still ends `DONE` with exit code `0`.

This matters for `--watch`: a long-running daemon must never crash
on a single bad git interaction.

### Resolution order

```
--post-fix CLI override   >   .autofix/config.json::post_fix   >   working-tree (default)
```

Unknown values at any layer fall back to default with a stderr
warning.

## `--watch` mode

`autofix run --watch` subscribes to a Watchman session under
`--root` and runs one full workflow cycle per change batch. Each
cycle creates its own `StateMachine` (so per-cycle JSONL logs
accumulate under `.autofix/runs/`).

### Per-cycle exception isolation

The watch dispatcher catches every `Exception` raised by
`_run_one_cycle` and logs it to stderr (`autofix: cycle <n>
raised <repr>`). The loop continues to the next batch.
`KeyboardInterrupt` and `SystemExit` propagate — Ctrl-C exits
cleanly with code `0`.

### Safety-sweep deadline

`--safety-sweep <Nh|Nm>` forces a full-cycle dispatch when no
Watchman events arrive within the threshold. Without it, a quiet
repo could go indefinitely between cycles even though state has
drifted.

### Test escape hatch

`AUTOFIX_WATCH_ONCE=1` makes the watch loop dispatch at most one
batch and exit `0`. Used by the test suite; rarely useful in
production.

## Combinatorics

| Flag combination | Behavior |
|---|---|
| (none) | Preview: scan + plan, exit `3` |
| `--suggest` | Print LLM diffs to stdout, exit `3` |
| `--apply` | Deterministic deletions, verify, exit `0`/`1` |
| `--apply --auto-llm` | Deterministic + LLM patches, recovery branch, verify |
| `--apply --suggest` | Preview-only LLM patches printed to stdout (no source mutation) |
| `--suggest --auto-llm` | **Rejected** — mutually exclusive (`--auto-llm` requires `--apply`) |
| `--auto-llm` (no `--apply`) | **Rejected** — `--auto-llm` requires `--apply` |
| `--watch` (any combination) | Compatible. Validates above ONCE before any session work. |
| `--max-retries 0` | One `APPLYING → VERIFYING` pass; no retry. |
| `--max-retries -1` | **Rejected** — must be non-negative. |

Combinatorics validation runs ONCE at the top of `run()`, before
any state machine or watcher work. Bad flag combinations exit `2`
(usage error) regardless of `--watch`.

## Debugging a stuck run

1. **Find the run-id.** Each run prints it on the first stderr
   `autofix: SCANNING ...` line, or look at the most recent
   directory under `.autofix/runs/`.

2. **Read the JSONL log.**
   ```bash
   cat .autofix/runs/<run-id>/state.jsonl | python3 -m json.tool --no-ensure-ascii
   ```
   Each row tells you when a state was entered, the SHA-256
   evidence hash of what crossed that edge, and the reason
   (when applicable).

3. **Check the verify log.**
   ```bash
   cat .autofix/runs/<run-id>/verify-<attempt>.log
   ```
   Contains `exit_code=<n>` plus full stdout/stderr from the test
   runner.

4. **Replay the scan.**
   ```bash
   autofix replay --scan-id <scan-id-from-events> --root .
   ```
   Verdicts: `match` (deterministic), `mismatch` (analyzer
   changed), `version_drift` (toolchain pinned-version mismatch).

5. **Inspect the recovery branch.**
   ```bash
   git branch | grep autofix/
   git diff autofix/pre-fix-snapshot-<utc>..HEAD
   ```

6. **If `--watch` is wedged**, the safety-sweep deadline forces a
   fresh dispatch. Hit Ctrl-C and re-run with
   `--safety-sweep 1m` to short-circuit.
