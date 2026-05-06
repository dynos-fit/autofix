"""LLM-scheduler subpackage (AC #1).

Only :mod:`autofix.llm.scheduler` is allowed to import from
``autofix.llm_backend``; AC #8 enforces that via a grep test. Everything
else under ``autofix/`` reaches the LLM boundary through the
:class:`~autofix.llm.scheduler.Scheduler` seam.
"""
