# Agentic LLM Backends

This document explains the low-token LLM execution model added to `autofix`.

## Why this exists

The original fix flow sends a large repair prompt to Claude with:

- the finding description
- structured evidence
- code around the finding
- neighbor-file context
- detector context
- past fix templates
- related findings

That preserves quality, but it also makes each repair request expensive.

The new design keeps the old `claude_cli` path available, while adding an `openai_compatible` path that behaves more like Codex or Claude Code:

- the model starts with a compact task prompt
- it reads files on demand
- it searches only when needed
- it runs narrow verification commands when helpful
- it finishes once it has made a focused change

This reduces prompt bloat without forcing us to give up the existing Claude-based workflow.

## Backends

`autofix` now supports two backend modes:

1. `claude_cli`
2. `openai_compatible`

`claude_cli` uses the installed `claude` command and keeps the original repair experience.

`openai_compatible` sends chat-completions requests to a configurable base URL. This works with local tools such as Ollama or `llama.cpp`, and with any server that exposes an OpenAI-style `/v1/chat/completions` API.

## Review flow

For `claude_cli`, review behavior stays close to the original implementation:

- files are selected by the crawler
- large files may be chunked
- each chunk or file is reviewed through prompt-based JSON output
- malformed JSON is repaired or regenerated

For `openai_compatible`, review uses a bounded agent loop:

- the model gets a target file
- it can list files, read slices of files, and search within the repo
- it must return `finish_review` with structured findings

This makes the model fetch only the context it needs.

## Fix flow

For `claude_cli`, autofix still builds a rich prompt and hands it to Claude with the existing permissioned tool setup.

For `openai_compatible`, autofix uses a bounded local agent loop:

- the model receives the finding, evidence, and focused context
- it can inspect files on demand
- it can edit files via `write_file` or `replace_text`
- it can run narrow verification commands such as `pytest`
- it must return `finish` when done

The backend then performs the same Git and PR handling as the original flow.

## Safety model

The local agent loop intentionally blocks internal control paths:

- `.git/`
- `.autofix/`
- `.dynos/`

It also restricts shell execution to a small allowlist:

- `pytest`
- `python -m pytest`
- `python3 -m pytest`
- `git diff`
- `git status`
- `git log`

That is not as powerful as a native Codex or Claude Code integration, but it is much safer than exposing arbitrary repo writes and arbitrary shell.

## Config keys

These config keys are relevant to the new backend:

- `llm_backend`
- `llm_base_url`
- `llm_api_key`
- `review_model`
- `fix_model`
- `llm_max_steps`
- `review_chunk_lines`
- `review_file_truncation`
- `fix_surrounding_lines`
- `fix_neighbor_files`
- `fix_neighbor_lines`

## Operational notes

- `autofix init` no longer requires `claude` to exist on the machine.
- `autofix scan` still enforces `claude` when `llm_backend=claude_cli`.
- regression rechecks now use the configured backend instead of assuming Claude is installed.

## Maintenance guidance

If you change the agent loop, keep these priorities in order:

1. Prevent writes to internal repo state directories.
2. Keep the shell allowlist narrow.
3. Preserve the existing `claude_cli` path as the quality baseline.
4. Add tests for every new tool action or backend-specific branch.
