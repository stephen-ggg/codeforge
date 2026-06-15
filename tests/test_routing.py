"""
Recovery-routing tests.

Guards the auto-recovery layer: a recoverable test-runner `error` is routed back to the
agent that owns the fix — keyed on the runner's deterministic error_phase — before
escalating, bounded by a dedicated retry budget. Also covers the one-shot low-confidence
re-prompt.
"""
from __future__ import annotations

from codeforge.orchestrator.routing import (
    route_low_confidence_reprompt,
    route_test_analysis_recoverable_error,
)
from codeforge.schemas.contracts import RetryCounters

_CONFIG = {
    "retry_limits": {
        "dependency_repair": 2,
        "environment_repair": 2,
        "low_confidence_reprompt": 1,
    }
}


# ----------------------------------------------------------------------
# error_phase → fixing agent
# ----------------------------------------------------------------------


def test_runtime_dep_phase_routes_to_coder() -> None:
    for phase in ("missing_requirements_txt", "runtime_dep_install_failed"):
        out = route_test_analysis_recoverable_error(phase, RetryCounters(), _CONFIG)
        assert out is not None, phase
        assert out.decision == "retry_same_agent"
        assert out.next_state == "coding"
        assert out.counter_deltas == {"dependency_repair": 1}
        assert out.row_id == "test_error_runtime_dep_repair"
        assert out.detail == f"error_phase={phase}"


def test_test_infra_phase_routes_to_test_designer() -> None:
    for phase in ("test_dep_install_failed", "no_results_report", "pytest_exit_error"):
        out = route_test_analysis_recoverable_error(phase, RetryCounters(), _CONFIG)
        assert out is not None, phase
        assert out.next_state == "test_design"
        assert out.counter_deltas == {"environment_repair": 1}
        assert out.row_id == "test_error_test_infra_repair"


def test_over_budget_escalates() -> None:
    counters = RetryCounters(dependency_repair=2)  # at the limit
    out = route_test_analysis_recoverable_error("runtime_dep_install_failed", counters, _CONFIG)
    assert out is not None
    assert out.decision == "escalate"
    assert out.escalation_reason == "human_required"
    assert out.row_id.endswith("_exhausted")


def test_unmapped_phase_returns_none() -> None:
    # transient/corrupt output and "no phase" are not auto-recoverable
    assert route_test_analysis_recoverable_error("results_parse_error", RetryCounters(), _CONFIG) is None
    assert route_test_analysis_recoverable_error(None, RetryCounters(), _CONFIG) is None


# ----------------------------------------------------------------------
# low-confidence one-shot re-prompt
# ----------------------------------------------------------------------


def test_low_confidence_reprompt_in_budget() -> None:
    out = route_low_confidence_reprompt("coder", RetryCounters(), _CONFIG)
    assert out.decision == "re_prompt_same_agent"
    assert out.next_state == "coder_reprompt"
    assert out.counter_deltas == {"low_confidence_reprompt": 1}
    assert out.escalation_reason is None


def test_low_confidence_reprompt_exhausted_escalates() -> None:
    counters = RetryCounters(low_confidence_reprompt=1)  # at the limit
    out = route_low_confidence_reprompt("coder", counters, _CONFIG)
    assert out.decision == "escalate"
    assert out.escalation_reason == "low_confidence"
    assert out.row_id == "low_confidence_reprompt_exhausted"
