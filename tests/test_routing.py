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
    route_test_analysis_code_bug,
    route_test_analysis_recoverable_error,
    route_test_analysis_spec_gap,
    route_test_analysis_test_bug,
    route_truncated,
)
from codeforge.schemas.contracts import RetryCounters

_CONFIG = {
    "retry_limits": {
        "dependency_repair": 2,
        "environment_repair": 2,
        "low_confidence_reprompt": 1,
        "test_loop": 2,
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
    assert out.counter_deltas == {"coder_low_confidence_reprompt": 1}
    assert out.escalation_reason is None


def test_low_confidence_reprompt_exhausted_escalates() -> None:
    counters = RetryCounters(coder_low_confidence_reprompt=1)  # at the limit
    out = route_low_confidence_reprompt("coder", counters, _CONFIG)
    assert out.decision == "escalate"
    assert out.escalation_reason == "low_confidence"
    assert out.row_id == "low_confidence_reprompt_exhausted"


def test_low_confidence_reprompt_per_agent_independent() -> None:
    # Coder budget exhausted should not block security_reviewer.
    counters = RetryCounters(coder_low_confidence_reprompt=1)
    out = route_low_confidence_reprompt("security_reviewer", counters, _CONFIG)
    assert out.decision == "re_prompt_same_agent"
    assert out.counter_deltas == {"security_reviewer_low_confidence_reprompt": 1}


# ----------------------------------------------------------------------
# test_analysis retries reset the per-invocation re-prompt cushions
# ----------------------------------------------------------------------
#
# Each in-budget test_analysis retry re-invokes a fresh agent task, so the
# one-shot low_confidence_reprompt / malformed_output budgets must reset —
# otherwise a budget spent on an earlier pass denies the retry its own shot
# and it escalates immediately (see run-fd4c4124e356).

def test_test_bug_retry_resets_reprompt_cushions() -> None:
    out = route_test_analysis_test_bug(RetryCounters(test_loop=0), _CONFIG)
    assert out.decision == "retry_same_agent"
    assert out.next_state == "test_design"
    assert "test_designer_low_confidence_reprompt" in out.counter_resets
    assert "malformed_output" in out.counter_resets


def test_code_bug_retry_resets_reprompt_cushions() -> None:
    out = route_test_analysis_code_bug(RetryCounters(test_loop=0), _CONFIG)
    assert out.next_state == "coding"
    assert "coder_low_confidence_reprompt" in out.counter_resets
    assert "malformed_output" in out.counter_resets


def test_spec_gap_retry_resets_reprompt_cushions_and_review_loops() -> None:
    out = route_test_analysis_spec_gap(RetryCounters(test_loop=0), _CONFIG)
    assert out.next_state == "architecture"
    # keeps its original review-loop resets …
    assert "code_review_loop" in out.counter_resets
    assert "security_review_loop" in out.counter_resets
    # … and restores per-agent cushions for all downstream agents.
    assert "architecture_designer_low_confidence_reprompt" in out.counter_resets
    assert "coder_low_confidence_reprompt" in out.counter_resets
    assert "code_reviewer_low_confidence_reprompt" in out.counter_resets
    assert "security_reviewer_low_confidence_reprompt" in out.counter_resets
    assert "malformed_output" in out.counter_resets


# ----------------------------------------------------------------------
# test_loop budget boundary: the last allowed retry must route onward, not
# escalate. The routing function is the authoritative decision; the
# run_test_design entry check must not preemptively block a cycle that the
# routing function approved (regression guard for the >= vs > off-by-one).
# With test_loop limit=2 the routing functions should:
#   counter=0 → route (0 failures used)
#   counter=1 → route (1 failure used, one more cycle allowed)
#   counter=2 → escalate (limit exhausted)
# ----------------------------------------------------------------------

def test_code_bug_routes_at_limit_minus_one() -> None:
    """Counter at limit-1 must still route to coding, not escalate."""
    out = route_test_analysis_code_bug(RetryCounters(test_loop=1), _CONFIG)
    assert out.decision == "retry_same_agent"
    assert out.next_state == "coding"


def test_code_bug_escalates_at_limit() -> None:
    out = route_test_analysis_code_bug(RetryCounters(test_loop=2), _CONFIG)
    assert out.decision == "escalate"


def test_test_bug_routes_at_limit_minus_one() -> None:
    out = route_test_analysis_test_bug(RetryCounters(test_loop=1), _CONFIG)
    assert out.decision == "retry_same_agent"
    assert out.next_state == "test_design"


def test_test_bug_escalates_at_limit() -> None:
    out = route_test_analysis_test_bug(RetryCounters(test_loop=2), _CONFIG)
    assert out.decision == "escalate"


def test_spec_gap_routes_at_limit_minus_one() -> None:
    out = route_test_analysis_spec_gap(RetryCounters(test_loop=1), _CONFIG)
    assert out.decision == "retry_same_agent"
    assert out.next_state == "architecture"


def test_spec_gap_escalates_at_limit() -> None:
    out = route_test_analysis_spec_gap(RetryCounters(test_loop=2), _CONFIG)
    assert out.decision == "escalate"


# ----------------------------------------------------------------------
# finish_reason=length truncation
# ----------------------------------------------------------------------

_TRUNC_CONFIG = {"retry_limits": {"truncation_retries": 1}}


def test_truncation_first_failure_reprompts_on_own_budget() -> None:
    out = route_truncated(RetryCounters(), _TRUNC_CONFIG, "test_designer")
    assert out.decision == "re_prompt_same_agent"
    assert out.next_state == "test_designer_reprompt"
    assert out.row_id == "output_truncated_retry"
    assert out.counter_deltas == {"truncation_retry": 1}
    # The malformed budget is never touched by the truncation path.
    assert "malformed_output" not in out.counter_deltas


def test_consecutive_truncation_escalates_as_output_truncated() -> None:
    out = route_truncated(RetryCounters(truncation_retry=1), _TRUNC_CONFIG, "test_designer")
    assert out.decision == "escalate"
    assert out.next_state == "failed_escalated"
    assert out.row_id == "output_truncated"
    # The reason must be output_truncated, not malformed_output, so the operator is
    # steered to raise max_tokens / shrink the unit of work rather than reset-and-retry.
    assert out.escalation_reason == "output_truncated"
