"""autofix_next — next-generation events-ingress vertical slice.

This package is the successor to the legacy ``autofix/`` scanner. It is being
built up vertically (events -> evidence -> LLM seam -> SARIF -> CLI) and must
not import from ``autofix.*`` at runtime except through explicit, documented
seams introduced in later segments.
"""

from __future__ import annotations

__version__ = "0.1.0"
