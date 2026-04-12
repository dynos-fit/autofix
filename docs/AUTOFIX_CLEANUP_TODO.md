# Autofix Cleanup TODO

## Completed

- [x] Make backend selection real in runtime wiring instead of documenting config keys that are ignored.
- [x] Support the documented backend-related config keys in `autofix config set/show/resolve`.
- [x] Wire `openai_compatible` review and fix execution into production paths.
- [x] Stop requiring `claude` for `autofix init`.
- [x] Fix `scan-all` so it builds a real runtime instead of failing behind a broad exception handler.
- [x] Use one shared repo scan lock for foreground scans, daemon scans, and `scan-all`.
- [x] Remove the dead merged-branch cleanup hook from the scan hot path.
- [x] Reject `interval=0m` so the daemon cannot hot-loop.
- [x] Update tests to validate the corrected contracts.
- [x] Run syntax verification with `python -m py_compile autofix/*.py tests/*.py`.
- [x] Run the full test suite with `pytest -q`.
- [x] Isolate the autofix cleanup changes from unrelated benchmark cleanup changes in the worktree.

## Follow-Ups

- [ ] Decide whether `repair_llm_output` and `regenerate_llm_output` should also become backend-agnostic or remain Claude-only repair helpers.
- [ ] Add higher-level integration tests that exercise full `openai_compatible` review/fix flows end to end with a fake backend.
- [ ] Revisit backend-specific timeout policy so `llm_timeout` and long Claude fix runs have one explicit contract.
