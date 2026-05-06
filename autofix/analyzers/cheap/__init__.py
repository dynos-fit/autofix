"""Cheap analyzers — per-file rules that only need a symbol table.

Currently: ``unused_import.intra-file``. Additional rules land here as
the funnel widens; each rule module must expose ``RULE_ID``,
``RULE_VERSION``, and ``analyze(parse_result, symbol_table) -> list[CandidateFinding]``.
"""
