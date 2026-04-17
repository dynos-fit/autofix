"""Indexing subpackage.

Per-file symbol-table construction plus the persistent SCIP index.

The subpackage ships two cooperating components:

* :mod:`autofix_next.indexing.symbols` — walks a ``ParseResult`` once and
  emits import records, identifier references, and the module-level
  ``__all__`` list the cheap analyzers depend on.
* :mod:`autofix_next.indexing.scip_emitter` — pure serializer that turns
  a slice of an in-memory :class:`CallGraph` into a ``scip_json_v1``
  shard ``dict``.
* :mod:`autofix_next.indexing.scip_index` — the persistent,
  content-addressed on-disk cache that wraps ``CallGraph`` with
  ``load / save / apply_incremental / get_symbol``.
"""

from autofix_next.indexing.scip_emitter import (
    SCIP_JSON_SCHEMA_VERSION,
    emit_document,
)
from autofix_next.indexing.scip_index import SCIPIndex

__all__ = [
    "SCIP_JSON_SCHEMA_VERSION",
    "SCIPIndex",
    "emit_document",
]
