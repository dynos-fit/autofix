"""LLM-scheduler subpackage (AC #1).

Only :mod:`autofix_next.llm.scheduler` is allowed to import from
``autofix.llm_backend``; AC #8 enforces that via a grep test. Everything
else under ``autofix_next/`` reaches the LLM boundary through the
:class:`~autofix_next.llm.scheduler.Scheduler` seam.
"""
