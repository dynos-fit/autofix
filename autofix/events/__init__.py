"""autofix.events — event-name enum and ScanEvent/ChangeSet dataclasses.

The canonical event-name set and payload shapes live here. Downstream
telemetry writers (``autofix.telemetry.events_log``) depend on this
module and must never invent ad-hoc event names.
"""
