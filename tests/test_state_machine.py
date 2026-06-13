"""
State machine invariant tests.

Guards the pending-writes invariant: no writes touch project-state/ on disk
before the Phase 6 flush. Everything staged during a run lives in memory only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeforge.config.config_loader import ConfigSnapshot
from codeforge.orchestrator.gates import GateResult
from codeforge.orchestrator.routing import (
    route_low_confidence,
    route_test_analysis_recoverable_error,
)
from codeforge.orchestrator.state_machine import (
    EscalationError,
    StateMachine,
    _build_dep_fix_context,
    _build_env_fix_context,
)
from codeforge.schemas.contracts import (
    AgentOutput,
    FailureAnalysis,
    Flag,
    LowConfidenceRePrompt,
    TestAnalysis,
    TestRunnerResults,
)


@pytest.fixture
def sm(minimal_config: ConfigSnapshot, project_dir: Path, run_log_dir: Path) -> StateMachine:
    machine = StateMachine(minimal_config, project_dir, run_log_dir)
    machine.start_run("new_project", "a brief")
    return machine


def _gate_events(run_log_dir: Path, run_id: str) -> list[dict]:
    path = run_log_dir / run_id / "events.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()]
    return [e for e in events if e.get("event_type") == "gate"]


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


# ----------------------------------------------------------------------
# Terminal policy escalation: rich, linked diagnostics
# ----------------------------------------------------------------------


def _block_output(summary: str) -> AgentOutput:
    """AgentOutput carrying a severity=block flag and a summary-bearing payload."""
    return AgentOutput(
        output={"verdict": "error", "summary": summary},
        assumptions_made=[],
        confidence=0.97,
        unresolved_flags=[
            Flag(
                id="FLAG-001",
                description="pytest-json-report plugin is not installed",
                severity="block",
                suggested_action="Add pytest-json-report to test deps",
            )
        ],
    )


def _block_gate_result(output: AgentOutput) -> GateResult:
    gr = GateResult()
    gr.policy_passed = False
    gr.escalation_reason = "block_flag"
    gr.policy_gate_rule = "block_flag_present"
    gr.parsed_output = output
    return gr


def test_block_flag_persists_links_and_enriches(sm: StateMachine, run_log_dir: Path) -> None:
    summary = "pytest exited with a fatal argument error before collecting any tests."
    output = _block_output(summary)
    gr = _block_gate_result(output)

    with pytest.raises(EscalationError) as exc:
        sm._handle_policy_escalation(
            gr, "test_analyst", "test_analysis", route_low_confidence("test_analyst")
        )
    assert exc.value.reason == "block_flag"

    # Gate event is self-sufficient and linked to a real artifact id.
    gate = _gate_events(run_log_dir, sm.run.run_id)[-1]
    assert gate["rule"] == "block_flag_present"
    assert gate["passed"] is False
    assert gate["source_agent"] == "test_analyst"
    artifact_id = gate["artifact_ref"]
    assert artifact_id
    assert "FLAG-001" in gate["detail"]
    assert "pytest-json-report plugin is not installed" in gate["detail"]
    assert summary in gate["detail"]

    # The escalation record points back to the same artifact.
    assert sm.run.escalations[-1].agent_output_ref == artifact_id

    # The artifact is persisted under failed_artifacts/ (recoverable for debugging).
    assert (run_log_dir / "failed_artifacts" / f"{artifact_id}.json").exists()

    # Side-effect guard: the blocked output must NOT leak into consumer queries
    # (assembler / resume) and must NOT occupy the normal artifacts slot.
    assert sm._artifact_store.exists("test_analysis") is False
    assert sm._artifact_store.get_latest("test_analysis") is None
    assert "test_analysis" not in sm.run.artifacts


def test_format_policy_detail_without_summary(sm: StateMachine) -> None:
    """Payloads lacking a summary degrade gracefully — no crash, segment omitted."""
    output = AgentOutput(
        output={"verdict": "error"},  # no 'summary' key
        assumptions_made=[],
        confidence=0.5,
        unresolved_flags=[Flag(id="F1", description="boom", severity="block")],
    )
    gr = _block_gate_result(output)

    detail = sm._format_policy_detail(gr, output)
    assert "escalation_reason=block_flag" in detail
    assert "F1" in detail
    assert "summary=" not in detail


def _low_conf_gate_result() -> GateResult:
    gr = GateResult()
    gr.policy_passed = False
    gr.escalation_reason = "low_confidence"
    gr.policy_gate_rule = "confidence_threshold"
    gr.parsed_output = AgentOutput(
        output={"verdict": "error", "summary": "uncertain result"},
        assumptions_made=[],
        confidence=0.10,
        unresolved_flags=[],
    )
    return gr


def test_low_confidence_reprompts_once_before_escalating(sm: StateMachine, run_log_dir: Path) -> None:
    # In budget: a low-confidence gate failure re-prompts (no escalation), increments the
    # dedicated counter, and returns a LowConfidenceRePrompt for the caller to retry with.
    reprompt = sm._handle_policy_escalation(
        _low_conf_gate_result(), "test_analyst", "test_analysis",
        route_low_confidence("test_analyst"),
    )
    assert isinstance(reprompt, LowConfidenceRePrompt)
    assert reprompt.reason == "low_confidence"
    assert sm.run.retry_counters.low_confidence_reprompt == 1

    gate = _gate_events(run_log_dir, sm.run.run_id)[-1]
    assert gate["rule"] == "confidence_threshold"
    assert "re-prompting" in gate["detail"]
    assert gate["artifact_ref"] is None  # not terminal -> not persisted


def test_low_confidence_escalates_when_reprompt_budget_exhausted(
    sm: StateMachine, run_log_dir: Path
) -> None:
    # Pre-exhaust the one-shot re-prompt budget so the next low-confidence failure is terminal.
    sm.run.retry_counters.low_confidence_reprompt = 1

    with pytest.raises(EscalationError) as exc:
        sm._handle_policy_escalation(
            _low_conf_gate_result(), "test_analyst", "test_analysis",
            route_low_confidence("test_analyst"),
        )
    assert exc.value.reason == "low_confidence"

    gate = _gate_events(run_log_dir, sm.run.run_id)[-1]
    assert gate["rule"] == "confidence_threshold"
    assert "confidence=0.1" in gate["detail"]
    assert "uncertain result" in gate["detail"]
    assert gate["artifact_ref"]  # terminal -> persisted + linked


# ----------------------------------------------------------------------
# Auto-recovery: environment error routes back to the test_designer
# ----------------------------------------------------------------------


def _environment_analysis() -> TestAnalysis:
    """A verdict=error analysis shaped like the real run-e92d024fe482 payload."""
    return TestAnalysis(
        verdict="error",
        summary="pytest exited with a fatal argument error before collecting any tests.",
        failure_analyses=[
            FailureAnalysis(
                test_case_id="ALL (no tests collected)",
                root_cause_hypothesis="environment",
                confidence=0.99,
                evidence="pytest stderr: unrecognized arguments --json-report",
                recommended_action="Add pytest-json-report>=1.5 to requirements-test.txt",
            )
        ],
        coverage_update=[],
    )


def test_build_env_fix_context_is_firewall_safe_projection() -> None:
    ctx = _build_env_fix_context(_environment_analysis())

    assert ctx["trigger"] == "test_error_environment"
    assert ctx["test_summary"].startswith("pytest exited")
    assert len(ctx["environment_findings"]) == 1
    finding = ctx["environment_findings"][0]
    # Only the whitelisted fields cross the firewall — no raw artifact, no code.
    assert set(finding.keys()) == {"recommended_action", "evidence"}
    assert "pytest-json-report" in finding["recommended_action"]
    # The raw analysis fields/keys must NOT leak through.
    assert "root_cause_hypothesis" not in finding
    assert "verdict" not in ctx
    assert "code_artifact" not in json.dumps(ctx)


def _routing_events(sm: StateMachine) -> list[dict]:
    return [
        json.loads(line)
        for line in (sm._run_log_dir / sm.run.run_id / "events.jsonl").read_text().splitlines()
        if json.loads(line).get("event_type") == "routing"
    ]


def test_test_infra_error_recovers_to_test_design_and_counts(sm: StateMachine) -> None:
    recovery = route_test_analysis_recoverable_error(
        "no_results_json", sm.run.retry_counters, sm._config.to_dict()
    )
    assert recovery is not None and recovery.next_state == "test_design"

    sm._apply_outcome(recovery)
    assert sm.run.retry_counters.environment_repair == 1
    assert sm.run.retry_counters.infrastructure == 0

    routing = _routing_events(sm)
    assert routing[-1]["routing_table_row"] == "test_error_test_infra_repair"
    assert "error_phase=no_results_json" in routing[-1]["detail"]


def test_runtime_dep_error_recovers_to_coding_with_dep_context(sm: StateMachine) -> None:
    recovery = route_test_analysis_recoverable_error(
        "runtime_dep_install_failed", sm.run.retry_counters, sm._config.to_dict()
    )
    assert recovery is not None
    assert recovery.next_state == "coding"

    sm._apply_outcome(recovery)
    assert sm.run.retry_counters.dependency_repair == 1
    assert _routing_events(sm)[-1]["routing_table_row"] == "test_error_runtime_dep_repair"

    # The coder fix context carries the runner's own stderr (no firewall projection needed).
    runner_results = TestRunnerResults(
        run_id="r1", started_at="t", completed_at="t", overall_status="error",
        test_results=[], environment_info={}, stdout_tail="",
        stderr_tail="ERROR: No matching distribution found for leftpad==9.9",
        error_phase="runtime_dep_install_failed",
    )
    ctx = _build_dep_fix_context(runner_results)
    assert ctx["trigger"] == "runtime_dep_error"
    assert "leftpad" in ctx["stderr_tail"]
