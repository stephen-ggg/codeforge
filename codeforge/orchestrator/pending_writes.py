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
import json
from typing import Any

from codeforge.schemas.contracts import ProjectStateDocument
from codeforge.store.project_state import ProjectStateStore

# Per-entry fields regenerated on every call (a fresh UUID, a fresh timestamp) and
# therefore useless for retry dedup — two appends of the *same* logical entry differ
# only in these. Excluded from the dedup signature so a retried append collapses.
_VOLATILE_ENTRY_FIELDS: frozenset[str] = frozenset({"entry_id", "created_at"})


def _entry_signature(entry: dict[str, Any]) -> str:
    """Stable identity of an append-only entry, ignoring volatile fields.

    decisions_log stamps a new entry_id (uuid4) and created_at on every append, so a
    retry of the same decision is byte-different; assumptions_log carries a stable id
    but no timestamp. Hashing the entry with the volatile fields stripped gives one
    signature that dedups both across retries while still distinguishing genuinely
    different entries (different decision/rationale, different assumption id).
    """
    stable = {k: v for k, v in entry.items() if k not in _VOLATILE_ENTRY_FIELDS}
    if not stable:
        # Entry carries only volatile fields — a degenerate stub. Fall back to the full
        # entry so distinct stubs aren't collapsed into one. Real decisions/assumptions
        # always carry stable content (decision/rationale, assumption id), so they still
        # dedup across retries on that content.
        stable = entry
    return json.dumps(stable, sort_keys=True, ensure_ascii=False)


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
        # Set whenever the staging map changes; consumed by the orchestrator before a run
        # snapshot so it only deep-copies/mirrors the map when there is something new to
        # persist (counter-only snapshots skip the copy entirely).
        self._dirty = False

    def set(self, document: ProjectStateDocument, data: dict[str, Any]) -> None:
        """Stage a full document write. Overwrites any prior pending write for this document."""
        self._map[document] = copy.deepcopy(data)
        self._dirty = True

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
        # The run already carries this exact map (it is where we restored from), so no
        # immediate re-sync is needed; leave the dirty flag untouched.

    def has(self, document: ProjectStateDocument) -> bool:
        """Return True if document has a pending write this run."""
        return document in self._map

    def consume_dirty(self) -> bool:
        """Return whether the staging map changed since the last call, then reset.

        Lets the orchestrator skip the get_all_changed() deepcopy on snapshots that
        didn't touch the staging map (the common counter/gate-update hot path).
        """
        was_dirty = self._dirty
        self._dirty = False
        return was_dirty

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

        Entries already present (by stable signature) are skipped, so re-running a
        step — gate re-prompts, code-bug reentry, resume — does not accumulate
        duplicate decisions/assumptions. The merged result is written back to the
        pending_writes map. Nothing touches disk here.
        """
        # Get current state — pending first, then disk
        current = self._map.get(document)
        if current is None:
            current = self._store.read(document)

        if current is None:
            # Document doesn't exist yet — create a minimal structure
            current = {"schema_version": "1.0.0", "entries": []}

        existing_entries: list[dict[str, Any]] = current.get("entries", [])
        seen: set[str] = {_entry_signature(e) for e in existing_entries}
        appended: list[dict[str, Any]] = []
        for entry in new_entries:
            sig = _entry_signature(entry)
            if sig in seen:
                continue  # exact retry of an already-staged entry — drop the duplicate
            seen.add(sig)
            appended.append(entry)

        merged = {**current, "entries": existing_entries + appended}
        self._map[document] = merged
        self._dirty = True
