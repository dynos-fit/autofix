"""Parsing subpackage.

Thin wrapper over ``tree_sitter`` + ``tree_sitter_python`` that produces
`ParseResult` records consumed by indexing and cheap analyzers. All
grammar-load failures are normalized into `TreeSitterLoadError` with a
diagnostic message that names the installed versions and the
remediation command.
"""
