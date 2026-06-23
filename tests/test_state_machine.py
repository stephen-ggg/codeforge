"""
State machine invariant tests.

Guards the pending-writes invariant: no writes touch project-state/ on disk
before the Phase 6 flush. Everything staged during a run lives in memory only.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
    MalformedOutputRePrompt,
    TestAnalysis,
    TestRunnerResults,
)


@pytest.fixture
def sm(minimal_config: ConfigSnapshot, project_dir: Path, run_log_dir: Path) -> StateMachine:
    machine = StateMachine(minimal_config, project_dir, run_log_dir)
    machine.start_run("new_project", "a brief")
    return machine


def _gate_events(run_log_dir: Path, run_id: str) -> list[dict[str, Any]]:
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


def _block_output(summary: str) -> AgentOutput[dict[str, str]]:
    """AgentOutput carrying a severity=block flag and a summary-bearing payload."""
    return AgentOutput(
        output={"verdict": "error", "summary": summary},
        assumptions_made=[],
        confidence=0.97,
        unresolved_flags=[
            Flag(
                id="FLAG-001",
                description="test collection requires a dependency missing from requirements-test.txt",
                severity="block",
                suggested_action="Add the missing dependency to requirements-test.txt",
            )
        ],
    )


def _block_gate_result(output: AgentOutput[dict[str, str]]) -> GateResult:
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
    assert "test collection requires a dependency missing from requirements-test.txt" in gate["detail"]
    assert summary in gate["detail"]

    # The escalation record points back to the same artifact.
    assert sm.run.escalations[-1].agent_output_ref == artifact_id

    # The artifact is persisted under the per-run failed_artifacts/ (recoverable for debugging).
    assert (run_log_dir / sm.run.run_id / "failed_artifacts" / f"{artifact_id}.json").exists()

    # Side-effect guard: the blocked output must NOT leak into consumer queries
    # (assembler / resume) and must NOT occupy the normal artifacts slot.
    assert sm._artifact_store.exists("test_analysis") is False
    assert sm._artifact_store.get_latest("test_analysis") is None
    assert "test_analysis" not in sm.run.artifacts


# ----------------------------------------------------------------------
# Structural / malformed escalation: the dropped raw output is recoverable
# ----------------------------------------------------------------------


def _malformed_gate_result() -> GateResult:
    gr = GateResult()
    gr.structural_passed = False
    gr.malformed_reprompt = MalformedOutputRePrompt(
        original_input_ref="assembly-1",
        validation_errors=[],
        attempt_number=1,
        max_attempts=2,
    )
    return gr


def test_malformed_exhausted_persists_raw_and_links(
    sm: StateMachine, run_log_dir: Path
) -> None:
    """On a budget-exhausted malformed failure the raw (unparseable) response is written
    to the isolated raw_outputs/ area and linked from the escalation, so the dropped
    output is debuggable instead of vanishing."""
    sm.run.retry_counters.malformed_output = 99  # force budget exhaustion
    gr = _malformed_gate_result()
    raw = '{"output": {"verdict": "pass"  <- truncated, not valid JSON'

    with pytest.raises(EscalationError) as exc:
        sm._handle_structural_failure(raw, "test_designer", gr)
    assert exc.value.reason == "malformed_output"

    # The escalation record points at a raw-output artifact id.
    artifact_id = sm.run.escalations[-1].agent_output_ref
    assert artifact_id

    # The dropped raw response is recoverable under the per-run raw_outputs/ (never artifacts/).
    raw_path = run_log_dir / sm.run.run_id / "raw_outputs" / f"{artifact_id}.json"
    assert raw_path.exists()
    record = json.loads(raw_path.read_text())
    assert record["raw"] == raw
    assert record["produced_by"] == "test_designer"


def test_truncated_output_falls_through_to_gate(
    sm: StateMachine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """finish_reason=length does not bypass gate evaluation.

    _invoke_agent returns the (truncated) content so the calling phase can route it
    through the gate. It records the truncation on _last_truncated so the structural
    failure handler can pick the bounded truncation path; it does not escalate here."""
    import logging
    from codeforge.model_router.router import RouterResult

    partial = '{"output": {"files": [{"path": "a.py", "content": "<cut off at max_tokens'
    monkeypatch.setattr(
        sm.router,
        "complete",
        lambda **kwargs: RouterResult(
            content=partial, litellm_call_id="call-1", model_used="m", truncated=True
        ),
    )

    with caplog.at_level(logging.WARNING, logger="codeforge.orchestrator.state_machine"):
        result = sm._invoke_agent("coder", "system", "turn")

    # Content is returned (not suppressed); caller drives gate evaluation.
    assert result == partial
    # The truncation is recorded for the structural failure handler.
    assert sm._last_truncated is True
    # Warning is emitted so finish_reason=length is visible in logs.
    assert any("finish_reason=length" in r.message for r in caplog.records)
    # No escalation was raised — the truncation/retry path owns that decision.
    assert sm.run.escalations == []


def test_truncated_output_first_failure_reprompts_on_own_budget(
    sm: StateMachine, run_log_dir: Path
) -> None:
    """A first finish_reason=length re-prompts (covering a transient hiccup) on the
    dedicated truncation_retry budget — it does NOT spend the malformed_output budget."""
    partial = '{"output": {"files": [{"path": "a.py", "content": "<cut off at max_tokens'
    gr = _malformed_gate_result()

    reprompt = sm._handle_structural_failure(partial, "coder", gr, truncated=True)

    assert reprompt is gr.malformed_reprompt
    assert sm.run.retry_counters.truncation_retry == 1
    assert sm.run.retry_counters.malformed_output == 0  # untouched
    assert sm.run.escalations == []
    # Within budget: nothing persisted (the output is not terminal).
    raw_dir = run_log_dir / sm.run.run_id / "raw_outputs"
    assert not any(raw_dir.glob("*.json"))


def test_consecutive_truncation_escalates_as_output_truncated(
    sm: StateMachine, run_log_dir: Path
) -> None:
    """A second, consecutive max_tokens truncation is genuine: escalate as
    output_truncated (not malformed_output) so the operator gets the right remedy
    (raise max_tokens / smaller unit of work) rather than a reset-and-retry loop."""
    partial = '{"output": {"files": [{"path": "a.py", "content": "<cut off at max_tokens'
    # First truncation already consumed the one-shot truncation_retry budget.
    sm.run.retry_counters.truncation_retry = 1
    gr = _malformed_gate_result()

    with pytest.raises(EscalationError) as exc:
        sm._handle_structural_failure(partial, "coder", gr, truncated=True)
    assert exc.value.reason == "output_truncated"

    # The partial response is persisted and linked from the escalation.
    artifact_id = sm.run.escalations[-1].agent_output_ref
    assert artifact_id
    raw_path = run_log_dir / sm.run.run_id / "raw_outputs" / f"{artifact_id}.json"
    assert raw_path.exists()
    assert json.loads(raw_path.read_text())["raw"] == partial


def test_malformed_detail_names_the_failing_field() -> None:
    """The schema_valid failure detail must name each failing field (path + error type +
    expectation) so events.jsonl alone diagnoses WHICH field is malformed."""
    from codeforge.orchestrator.gates import GateEvaluator
    from codeforge.schemas.contracts import ValidationError as VErr

    malformed = MalformedOutputRePrompt(
        original_input_ref="assembly-1",
        validation_errors=[
            VErr(
                field_path="output.confidence",
                error_type="missing_required",
                expected="Field required",
                received=None,
            )
        ],
        attempt_number=1,
        max_attempts=2,
    )
    detail = GateEvaluator._format_malformed_detail(malformed)
    assert "validation errors: 1" in detail
    assert "output.confidence" in detail
    assert "missing_required" in detail
    assert "Field required" in detail


def test_malformed_within_budget_reprompts_without_persisting(
    sm: StateMachine, run_log_dir: Path
) -> None:
    """While in budget a malformed failure re-prompts (no escalation, nothing persisted —
    the output is not terminal) and increments the malformed counter."""
    sm.run.retry_counters.malformed_output = 0
    gr = _malformed_gate_result()

    reprompt = sm._handle_structural_failure('{"bad": ', "coder", gr)

    assert reprompt is gr.malformed_reprompt
    assert sm.run.retry_counters.malformed_output == 1
    assert sm.run.escalations == []

    raw_dir = run_log_dir / sm.run.run_id / "raw_outputs"
    assert not any(raw_dir.glob("*.json"))


def test_malformed_exhausted_gate_links_raw(
    sm: StateMachine, run_log_dir: Path
) -> None:
    """On a malformed escalation the failing schema_valid gate event must carry the
    persisted raw output's id as artifact_ref, so events.jsonl alone links the gate line
    to the dropped output (not just the escalation record)."""
    sm.run.retry_counters.malformed_output = 99  # force budget exhaustion
    gr = _malformed_gate_result()

    with pytest.raises(EscalationError):
        sm._handle_structural_failure('{"bad": ', "coder", gr)

    artifact_id = sm.run.escalations[-1].agent_output_ref
    gate = _gate_events(run_log_dir, sm.run.run_id)[-1]
    assert gate["rule"] == "schema_valid"
    assert gate["passed"] is False
    assert gate["artifact_ref"] == artifact_id


def test_contract_failure_persists_output_to_failed_artifacts(
    sm: StateMachine, run_log_dir: Path
) -> None:
    """A contract-violating (but schema-valid) output is persisted to the isolated
    failed_artifacts/ area so a contract escalation links it instead of dropping it —
    and it must NOT leak into artifacts/ (where get_latest/resume would surface it)."""
    gr = GateResult()
    gr.contract_passed = False
    gr.parsed_output = AgentOutput(
        output={"files": []}, assumptions_made=[], confidence=0.9, unresolved_flags=[]
    )

    artifact_id = sm._persist_contract_failure(gr, "coder", "code_artifact")
    assert artifact_id

    run_dir = run_log_dir / sm.run.run_id
    assert (run_dir / "failed_artifacts" / f"{artifact_id}.json").exists()
    assert not (run_dir / "artifacts" / f"{artifact_id}.json").exists()


def test_requirements_contract_failure_is_not_accepted_as_success(
    sm: StateMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema-valid but contract-violating requirements output must route through the
    re-prompt/escalate path — not fall through both guards and be accepted as a valid
    doc. Regression: run_requirements had no contract_passed branch, so a contract
    failure (which leaves policy_passed defaulting True) was silently treated as success."""
    import types
    from codeforge.agents.requirements_analyst import RequirementsAnalystAgent

    fake_pkg = types.SimpleNamespace(state_documents={}, assembly_id="asm-1")
    monkeypatch.setattr(sm.assembler, "assemble", lambda *a, **k: fake_pkg)
    monkeypatch.setattr(
        RequirementsAnalystAgent, "build_user_turn", lambda self, pkg, reprompt: "turn"
    )
    monkeypatch.setattr(sm, "_invoke_agent", lambda *a, **k: '{"status": "complete"}')

    gr = GateResult()
    gr.contract_passed = False  # structural_passed/policy_passed stay True
    monkeypatch.setattr(sm.gates, "evaluate", lambda **k: gr)

    # Exhaust the shared malformed budget so the contract failure escalates (via
    # route_malformed) rather than looping forever on re-prompts.
    limit = sm._config.to_dict().get("retry_limits", {}).get("malformed_output_retries", 2)
    sm.run.retry_counters = sm.run.retry_counters.model_copy(
        update={"malformed_output": limit}
    )

    with pytest.raises(EscalationError):
        sm.run_requirements("brief", object(), "system prompt")


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
    assert sm.run.retry_counters.test_analyst_low_confidence_reprompt == 1

    gate = _gate_events(run_log_dir, sm.run.run_id)[-1]
    assert gate["rule"] == "confidence_threshold"
    assert "re-prompting" in gate["detail"]
    assert gate["artifact_ref"] is None  # not terminal -> not persisted


def test_low_confidence_escalates_when_reprompt_budget_exhausted(
    sm: StateMachine, run_log_dir: Path
) -> None:
    # Pre-exhaust the one-shot re-prompt budget so the next low-confidence failure is terminal.
    sm.run.retry_counters.test_analyst_low_confidence_reprompt = 1

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
                evidence="pytest collection error: ModuleNotFoundError: No module named 'httpx'",
                recommended_action="Add httpx>=0.27 to requirements-test.txt",
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
    assert "httpx" in finding["recommended_action"]
    # The raw analysis fields/keys must NOT leak through.
    assert "root_cause_hypothesis" not in finding
    assert "verdict" not in ctx
    assert "code_artifact" not in json.dumps(ctx)


def _routing_events(sm: StateMachine) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (sm._run_log_dir / sm.run.run_id / "events.jsonl").read_text().splitlines()
        if json.loads(line).get("event_type") == "routing"
    ]


def test_test_infra_error_recovers_to_test_design_and_counts(sm: StateMachine) -> None:
    recovery = route_test_analysis_recoverable_error(
        "no_results_report", sm.run.retry_counters, sm._config.to_dict()
    )
    assert recovery is not None and recovery.next_state == "test_design"

    sm._apply_outcome(recovery)
    assert sm.run.retry_counters.environment_repair == 1
    assert sm.run.retry_counters.infrastructure == 0

    routing = _routing_events(sm)
    assert routing[-1]["routing_table_row"] == "test_error_test_infra_repair"
    assert "error_phase=no_results_report" in routing[-1]["detail"]


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
