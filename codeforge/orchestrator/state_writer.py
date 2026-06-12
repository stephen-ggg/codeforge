"""
orchestrator/state_writer.py — Phase 6 pending_writes flush.

The ONLY place that triggers disk writes to project-state/.
Called once on codeforge success — never mid-run.

A failed run leaves project-state/ on disk completely untouched
because this module is never called on failure paths.
"""

from __future__ import annotations

import hashlib
import json

from codeforge.orchestrator.pending_writes import PendingWrites
from codeforge.orchestrator.event_log import EventLog
from codeforge.schemas.contracts import CountersSnapshot
from codeforge.store.project_state import ProjectStateStore


def flush_pending_writes(
    pending: PendingWrites,
    project_state: ProjectStateStore,
    event_log: EventLog,
    counters: CountersSnapshot,
    run_id: str = "",
) -> list[str]:
    """
    Write all staged documents from pending_writes to disk as JSON + markdown pairs.

    requirements_history is routed to write_requirements_history(run_id, data) because
    it is a per-run-id file rather than a singleton document.

    Returns the list of document names that were written.
    Emits a state_write event for each document.

    This is called exactly once per successful codeforge run, from Phase 6.
    Nothing else should call this function.
    """
    changed = pending.get_all_changed()
    written: list[str] = []

    for document_str, data in changed.items():
        if document_str == "requirements_history":
            # Per-run-id file — use the dedicated write path.
            existing_rh = project_state.read_requirements_history(run_id)
            before_hash = _hash(existing_rh) if existing_rh else "none"
            project_state.write_requirements_history(run_id, data)
            after_hash = _hash(data)
        else:
            existing = project_state.read(document_str)  # type: ignore[arg-type]
            before_hash = _hash(existing) if existing else "none"
            project_state.write(document_str, data)  # type: ignore[arg-type]
            after_hash = _hash(data)

        written.append(document_str)
        event_log.emit_state_write(
            document=document_str,  # type: ignore[arg-type]
            write_source="codeforge_success",
            gate_condition="all_phases_passed",
            content_hash_before=before_hash,
            content_hash_after=after_hash,
            counters=counters,
        )

    return written


def _hash(data: object) -> str:
    """SHA-256 of the JSON-serialised data."""
    serialised = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serialised.encode()).hexdigest()
