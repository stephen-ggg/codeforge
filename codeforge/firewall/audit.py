"""
firewall/audit.py — Access event emission.

Thin helper that writes an AccessEvent to the orchestrator event log.
Kept separate so the assembler doesn't need to import the full event log.
"""

from __future__ import annotations

from codeforge.schemas.contracts import AccessEvent


class EventLogProtocol:
    """
    Minimal interface the audit module needs from the event log.
    The concrete EventLog is implemented in Stage 6 (orchestrator/event_log.py).
    Defined here as a base class so audit.py has no circular dependency.
    """

    def emit_access_event(self, event: AccessEvent) -> None:
        """Write an AccessEvent to the append-only event log."""
        raise NotImplementedError


def log_access_event(event: AccessEvent, event_log: EventLogProtocol) -> None:
    """
    Emit an AccessEvent to the orchestrator event log.

    Called by the assembler after every access decision — both allow and deny.
    The event log is append-only; this call never modifies or deletes entries.
    """
    event_log.emit_access_event(event)
