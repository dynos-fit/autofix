"""Invalidation planner subpackage (AC #1).

Future roadmap will replace the stub :mod:`autofix_next.invalidation.planner`
with a real dependency-aware plan that prunes the changeset to the minimal
set of files whose analysis results could have changed since the last scan.
For now the planner is a pass-through so the funnel pipeline can wire the
seam in without a behavioral change.
"""
