"""Funnel-pipeline subpackage (AC #1).

Provides the :func:`autofix_next.funnel.pipeline.run_scan` orchestrator
that wires the cheap analyzer layer, the evidence builder, and the LLM
scheduler into a single entry point. The CLI in a later segment imports
from here rather than reaching into the individual sub-layers.
"""
