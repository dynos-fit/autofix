"""Indexing subpackage.

Per-file symbol-table construction. Walks a `ParseResult` once and emits
the information the cheap analyzers need: import records (module,
bound-name, alias, line range, raw text), every non-import identifier
reference, and the module-level `__all__` list when present.
"""
