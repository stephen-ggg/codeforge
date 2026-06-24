"""
orchestrator/pending_writes.py — In-memory project state staging.

All project state document writes are staged here during a run.
Nothing is written to disk until Phase 6 flush on success.
A failed run leaves project-state/ on disk completely untouched.

Read priority (used by assembler and state machine):
  1. pending_writes map (current run's in-progress state)
  2. disk (prior run's committed state, read at run start)

Append-only documents (decisions_log, assumptions_log) accumulate entries
within a run via merge_append() — each append goes to pending_writes so
the full history is correct for agents reading mid-run.
"""

from __future__ import annotations

import copy
from typing import Any

from codeforge.schemas.contracts import ProjectStateDocument
from codeforge.store.project_state import ProjectStateStore


class PendingWrites:
    """
    In-memory staging map for project state document writes.

    The orchestrator reads from this first when assembling agent context,
    falling back to disk for documents not yet staged this run.
    """

    def __init__(self, project_state: ProjectStateStore) -> None:
        # The on-disk store — used as fallback for merge_append
        self._store = project_state
        self._map: dict[str, dict[str, Any]] = {}

    def set(self, document: ProjectStateDocument, data: dict[str, Any]) -> None:
        """Stage a full document write. Overwrites any prior pending write for this document."""
        self._map[document] = copy.deepcopy(data)

    def get(self, document: ProjectStateDocument) -> dict[str, Any] | None:
        """
        Return the pending write for document, or None if not staged this run.
        Callers fall back to disk when None is returned.
        """
        pending = self._map.get(document)
        if pending is None:
            return None
        return copy.deepcopy(pending)

    def get_all_changed(self) -> dict[str, dict[str, Any]]:
        """Return a copy of all staged documents. Used by Phase 6 flush."""
        return copy.deepcopy(self._map)

    def restore(self, data: dict[str, dict[str, Any]]) -> None:
        """Replace the staging map with a previously captured snapshot.

        Used on resume to rehydrate the writes a prior session staged (and that were
        persisted to codeforge_run.json) so an interrupted run can flush the complete
        project state at commit. Mirrors the shape returned by get_all_changed().
        """
        self._map = copy.deepcopy(data)

    def has(self, document: ProjectStateDocument) -> bool:
        """Return True if document has a pending write this run."""
        return document in self._map

    def merge_append(
        self,
        document: ProjectStateDocument,
        new_entries: list[dict[str, Any]],
    ) -> None:
        """
        Append new_entries to an append-only document (decisions_log, assumptions_log).

        Read priority:
          1. pending_writes map — accumulates within a run
          2. disk — prior committed state (baseline)

        The merged result is written back to the pending_writes map.
        Nothing touches disk here.
        """
        # Get current state — pending first, then disk
        current = self._map.get(document)
        if current is None:
            current = self._store.read(document)

        if current is None:
            # Document doesn't exist yet — create a minimal structure
            current = {"schema_version": "1.0.0", "entries": []}

        existing_entries: list[dict[str, Any]] = current.get("entries", [])
        merged = {**current, "entries": existing_entries + new_entries}
        self._map[document] = merged
