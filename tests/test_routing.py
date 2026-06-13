"""
Recovery-routing tests.

Guards the auto-recovery layer: a recoverable `error` verdict (e.g. an environment /
missing-test-dependency failure) is routed back to the agent that can fix it before
escalating to a human, bounded by a dedicated retry budget.
"""
from __future__ import annotations

from codeforge.orchestrator.routing import (
    _dominant_recoverable_cause,
    route_test_analysis_recoverable_error,
)
from codeforge.schemas.contracts import FailureAnalysis, RetryCounters, TestAnalysis


def _analysis(*causes: str) -> TestAnalysis:
    return TestAnalysis(
        verdict="error",
        summary="pytest could not collect tests",
        failure_analyses=[
            FailureAnalysis(
                test_case_id=f"TC-{i}",
                root_cause_hypothesis=cause,
                confidence=0.99,
                evidence="pytest: unrecognized arguments --json-report",
                recommended_action="Add pytest-json-report>=1.5 to requirements-test.txt",
            )
            for i, cause in enumerate(causes)
        ],
        coverage_update=[],
    )


_CONFIG = {"retry_limits": {"environment_repair": 2}}


def test_environment_within_budget_routes_to_test_design() -> None:
    outcome = route_test_analysis_recoverable_error(
        _analysis("environment"), RetryCounters(), _CONFIG
    )
    assert outcome is not None
    assert outcome.decision == "retry_same_agent"
    assert outcome.next_state == "test_design"
    assert outcome.counter_deltas == {"environment_repair": 1}
    assert outcome.extra["recovery_root_cause"] == "environment"
    assert outcome.escalation_reason is None


def test_environment_over_budget_escalates() -> None:
    counters = RetryCounters(environment_repair=2)  # at the limit
    outcome = route_test_analysis_recoverable_error(_analysis("environment"), counters, _CONFIG)
    assert outcome is not None
    assert outcome.decision == "escalate"
    assert outcome.escalation_reason == "human_required"
    assert outcome.row_id.endswith("_exhausted")
    assert outcome.next_state == "failed_escalated"


def test_non_recoverable_cause_returns_none() -> None:
    # 'ambiguous' has no recovery route; empty failures likewise.
    assert route_test_analysis_recoverable_error(_analysis("ambiguous"), RetryCounters(), _CONFIG) is None
    assert route_test_analysis_recoverable_error(_analysis(), RetryCounters(), _CONFIG) is None


def test_mixed_with_code_bug_is_not_auto_recovered() -> None:
    # A code_bug/spec_gap mixed in means it's not ours to silently repair.
    assert _dominant_recoverable_cause(_analysis("environment", "code_bug")) is None
    assert route_test_analysis_recoverable_error(
        _analysis("environment", "code_bug"), RetryCounters(), _CONFIG
    ) is None
    assert _dominant_recoverable_cause(_analysis("environment")) == "environment"
