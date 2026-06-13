"""
State machine invariant tests.

Guards the pending-writes invariant: no writes touch project-state/ on disk
before the Phase 6 flush. Everything staged during a run lives in memory only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from codeforge.config.config_loader import ConfigSnapshot
from codeforge.orchestrator.state_machine import StateMachine


@pytest.fixture
def sm(minimal_config: ConfigSnapshot, project_dir: Path, run_log_dir: Path) -> StateMachine:
    machine = StateMachine(minimal_config, project_dir, run_log_dir)
    machine.start_run("new_project", "a brief")
    return machine


def test_start_run_initialises_components(sm: StateMachine) -> None:
    assert sm.run is not None
    assert sm.pending is not None
    assert sm.event_log is not None


def test_pending_writes_are_memory_only(sm: StateMachine, project_dir: Path) -> None:
    sm.pending.set("architecture", {"schema_version": "1.0.0", "modules": []})
    sm.pending.set("tech_stack", {"schema_version": "1.0.0", "decisions": []})

    state_dir = project_dir / "project-state"
    on_disk = list(state_dir.rglob("*.json")) if state_dir.exists() else []
    assert on_disk == [], f"Unexpected disk writes before Phase 6: {on_disk}"


def test_event_log_writes_to_run_log_not_project_state(
    sm: StateMachine, project_dir: Path, run_log_dir: Path
) -> None:
    # start_run() calls update_run_snapshot() which writes codeforge_run.json to the run dir
    run_snapshot = run_log_dir / sm.run.run_id / "codeforge_run.json"
    assert run_snapshot.exists(), "codeforge_run.json should exist after start_run()"

    state_dir = project_dir / "project-state"
    on_disk = list(state_dir.rglob("*")) if state_dir.exists() else []
    assert on_disk == [], f"project-state/ should be empty before Phase 6: {on_disk}"


def test_escalation_captures_current_phase(sm: StateMachine) -> None:
    """_escalate() must stamp the phase that was running onto the EscalationEvent so
    handle_escalation() can suggest the correct reentry state to the operator."""
    import pytest
    from codeforge.orchestrator.state_machine import EscalationError

    sm._current_phase = "coding"
    with pytest.raises(EscalationError):
        sm._escalate("max_retries_exceeded", "test context")

    assert len(sm.run.escalations) == 1
    event = sm.run.escalations[0]
    assert event.suggested_reentry_state == "coding"


def test_escalation_without_phase_sets_none(sm: StateMachine) -> None:
    """When _current_phase is not set (e.g. pre-run escalation), suggested_reentry_state
    is None — handle_escalation() omits the suggestion gracefully."""
    import pytest
    from codeforge.orchestrator.state_machine import EscalationError

    assert sm._current_phase is None
    with pytest.raises(EscalationError):
        sm._escalate("global_ceiling_exceeded", "")

    event = sm.run.escalations[0]
    assert event.suggested_reentry_state is None
