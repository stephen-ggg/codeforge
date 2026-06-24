"""Pending-writes read-priority tests — see codeforge/orchestrator/pending_writes.py.

The documented read priority is:
  1. pending_writes map (current run's in-progress state)
  2. disk (prior run's committed state)

These exercise that priority against a REAL ProjectStateStore on disk — the case
no other test covered: a document present BOTH on disk and in pending must resolve
to the pending value, with the disk baseline ignored.
"""

from __future__ import annotations

from pathlib import Path

from codeforge.orchestrator.pending_writes import PendingWrites
from codeforge.store.project_state import ProjectStateStore


def _store(tmp_path: Path) -> ProjectStateStore:
    project = tmp_path / "project"
    project.mkdir()
    return ProjectStateStore(project)


def _disk_entry(entry_id: str, decision: str) -> dict:
    return {
        "entry_id": entry_id,
        "run_id": "r0",
        "entry_type": "agent_decision",
        "source_agent": "coder",
        "decision": decision,
        "rationale": "x",
        "created_at": "2026-06-15T00:00:00+00:00",
    }


def test_merge_append_uses_disk_baseline_when_nothing_pending(tmp_path: Path) -> None:
    """With no pending write, merge_append falls back to the committed disk state as the
    baseline and appends onto it."""
    store = _store(tmp_path)
    store.write(
        "decisions_log",
        {"schema_version": "1.0.0", "entries": [_disk_entry("d0", "disk baseline")]},
    )
    pending = PendingWrites(store)

    pending.merge_append("decisions_log", [{"entry_id": "d1"}])

    entries = pending.get("decisions_log")["entries"]
    assert [e["entry_id"] for e in entries] == ["d0", "d1"]


def test_merge_append_prefers_pending_over_disk(tmp_path: Path) -> None:
    """Disk holds a STALE baseline; once a pending write exists, merge_append must use the
    PENDING baseline and never read the stale disk value."""
    store = _store(tmp_path)
    store.write(
        "decisions_log",
        {"schema_version": "1.0.0", "entries": [_disk_entry("STALE", "stale disk")]},
    )
    pending = PendingWrites(store)
    # The current run already staged its own baseline this session.
    pending.set("decisions_log", {"schema_version": "1.0.0", "entries": [{"entry_id": "d0"}]})

    pending.merge_append("decisions_log", [{"entry_id": "d1"}])

    ids = [e["entry_id"] for e in pending.get("decisions_log")["entries"]]
    assert ids == ["d0", "d1"]
    assert "STALE" not in ids


def test_get_prefers_pending_over_disk(tmp_path: Path) -> None:
    """A full-document get via the staging map returns the pending value even when a
    different value is committed on disk — pending shadows disk within a run."""
    store = _store(tmp_path)
    store.write(
        "decisions_log",
        {"schema_version": "1.0.0", "entries": [_disk_entry("STALE", "stale disk")]},
    )
    pending = PendingWrites(store)
    pending.set("decisions_log", {"schema_version": "1.0.0", "entries": [{"entry_id": "fresh"}]})

    got = pending.get("decisions_log")
    assert [e["entry_id"] for e in got["entries"]] == ["fresh"]
    # Disk is untouched by staging (memory-only invariant).
    assert store.read("decisions_log")["entries"][0]["entry_id"] == "STALE"
