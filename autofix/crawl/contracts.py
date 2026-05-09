"""Public adapter contracts for the crawl subsystem.

The crawler's algorithm (``picker.pick_next_batch`` and
``bundles.expand_bundle``) is data-source agnostic — it consumes
two duck-typed adapters via dependency injection. This module
gives those adapters a typed, runtime-checkable contract.

Two consumers benefit from this typing:

1. **mypy --strict** can now verify at edit-time that any object
   passed as ``git_log=`` or ``call_graph=`` actually conforms to
   the picker's needs. Previously the parameter type was ``Any``.
2. **External integrators** (you, or someone lifting ``autofix.crawl``
   into another project) get a documented surface to implement
   against. Implement the four / one methods below and the crawler
   will accept your adapter without modification.

Both protocols are ``runtime_checkable`` — operators can write
``isinstance(obj, GitLogAdapter)`` to validate an injected adapter
at construction time. The runtime check verifies presence of the
named attributes only; it does NOT verify method signatures
(this is a documented Protocol limitation; see PEP 544).

Reference implementations live in
``autofix/cli/cycle_runner.py``:

* ``_GitLogAdapter`` — a git-subprocess-backed implementation with
  a stdlib-only fallback for non-git trees.
* ``CallGraphPathAdapter`` (in
  ``autofix/crawl/_call_graph_adapter.py``) — wraps the symbol-level
  ``autofix.invalidation.call_graph.CallGraph`` as a path-level
  adapter the bundle expander can consume.

External adapters can substitute either of these with no other code
changes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class GitLogAdapter(Protocol):
    """Read-only contract for the picker's git/file-history needs.

    Any object exposing these three methods can be passed as the
    ``git_log`` argument to ``pick_next_batch``. The picker uses
    them to score candidate seed paths via ``relevance(...)``:

    * ``list_candidate_files`` — enumerate scan candidates. The
      crawler is language-agnostic; the adapter returns every
      tracked path it wants the picker to consider, of any type.
      Downstream analyzers decide what to do with each file.
    * ``days_since_last_commit`` — recency subscore (60% weight).
    * ``commits_in_last_30_days`` — churn subscore (40% weight).

    Implementations may back these by ``git`` subprocess calls,
    GitHub API calls, an LSP server, or any other source — the
    crawler is agnostic. A pure stdlib fallback (``Path.rglob`` +
    ``stat().st_mtime``) is acceptable for non-git trees.

    Centrality (incoming-dependency count) was removed from the
    contract because it required language-specific import-graph
    walking. Files that are heavily depended on float to the top
    via ``churn`` instead — repeatedly-edited central files tend
    to win on that signal anyway.
    """

    def list_candidate_files(self) -> list[str]: ...

    def days_since_last_commit(self, path: str) -> int: ...

    def commits_in_last_30_days(self, path: str) -> int: ...


@runtime_checkable
class CallGraphAdapter(Protocol):
    """Read-only contract for the bundle expander's neighbor lookup.

    The single ``neighbors_of(path)`` method returns the union of
    callers + callees for a given file as a list of relative
    ``Path`` objects (callers ∪ callees, deduped).
    ``expand_bundle`` calls this once per BFS step.

    The reference implementation is ``CallGraphPathAdapter`` in
    ``autofix/crawl/_call_graph_adapter.py`` (a thin wrapper over
    the project's symbol-level ``CallGraph``). External integrators
    can substitute an LSP-backed adapter, a static-analyzer adapter,
    or a no-op adapter (returning ``[]`` for every input — the
    bundle then degenerates to a singleton seed, which is the
    default fall-soft behavior on a broken indexer).
    """

    def neighbors_of(self, path: Path) -> list[Path]: ...


__all__ = [
    "GitLogAdapter",
    "CallGraphAdapter",
]
