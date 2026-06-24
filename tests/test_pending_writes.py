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


def test_merge_append_dedups_retried_entry(tmp_path: Path) -> None:
    """A retried step re-appends the same logical decision with a FRESH entry_id and
    created_at (both regenerated per call). merge_append must dedup on stable content so
    the decisions_log doesn't accumulate a duplicate on every reentry."""
    store = _store(tmp_path)
    pending = PendingWrites(store)

    def decision_entry(entry_id: str, ts: str) -> dict:
        return {
            "entry_id": entry_id,            # volatile: new uuid each attempt
            "run_id": "r1",
            "entry_type": "agent_decision",
            "source_agent": "coder",
            "decision": "use sqlite",
            "rationale": "small footprint",
            "created_at": ts,                # volatile: new timestamp each attempt
        }

    pending.merge_append("decisions_log", [decision_entry("uuid-1", "2026-06-24T00:00:00+00:00")])
    # Same decision, retried — different entry_id AND created_at.
    pending.merge_append("decisions_log", [decision_entry("uuid-2", "2026-06-24T00:05:00+00:00")])

    entries = pending.get("decisions_log")["entries"]
    assert len(entries) == 1, "retried identical decision must not duplicate"

    # A genuinely different decision is still appended.
    pending.merge_append("decisions_log", [
        {**decision_entry("uuid-3", "2026-06-24T00:06:00+00:00"), "decision": "add redis"}
    ])
    assert len(pending.get("decisions_log")["entries"]) == 2


def test_merge_append_dedups_assumption_by_stable_id(tmp_path: Path) -> None:
    """Assumptions carry a stable id but no timestamp; a retry re-emits an identical
    entry, which must collapse rather than accumulate."""
    store = _store(tmp_path)
    pending = PendingWrites(store)
    entry = {
        "id": "ASSUME-001",
        "description": "auth is out of scope",
        "impact": "high",
        "record": True,
        "run_id": "r1",
        "source_agent": "coder",
        "status": "open",
    }
    pending.merge_append("assumptions_log", [entry])
    pending.merge_append("assumptions_log", [dict(entry)])
    assert len(pending.get("assumptions_log")["entries"]) == 1


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
