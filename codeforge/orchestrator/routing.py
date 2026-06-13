"""
orchestrator/routing.py — Codeforge routing table as code.

Each function corresponds to a named row in the spec routing table. No routing
logic lives in the state machine — all verdict-to-action mapping is here.

Routes are named after the phase they belong to (requirements, architecture,
coding, code_review, security_review, test_design, test_execution,
test_analysis, commit) rather than the old MVP phase numbers (P1, P2, ...).
The `row_id` each handler emits follows the same convention so an
``events.jsonl`` ``routing`` line names exactly what happened and which handler
produced it — e.g. ``"code_review_pass_with_notes"`` maps directly to
``route_code_review_pass`` here.

Each handler returns a RoutingOutcome describing:
  - The RoutingDecision (what the orchestrator should do next)
  - Counter deltas (which counters to increment)
  - Counter resets (which counters to zero)
  - next_state: the state label for the event log
  - row_id: the stable, self-describing routing table row identifier
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from codeforge.schemas.contracts import (
    AgentOutput,
    CodeArtifact,
    EscalationReason,
    RequirementsDoc,
    RetryCounters,
    ReviewReport,
    RoutingDecision,
    SecurityReport,
    TestAnalysis,
    ArchitectureDoc,
    TestSuite,
)


@dataclass
class RoutingOutcome:
    """The result of evaluating a routing table row."""
    row_id: str
    decision: RoutingDecision
    next_state: str
    counter_deltas: dict[str, int] = field(default_factory=dict)
    counter_resets: list[str] = field(default_factory=list)
    escalation_reason: EscalationReason | None = None
    # Structured context for the state machine to act on
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Budget helpers
# ---------------------------------------------------------------------------

def _within_budget(counter_value: int, limit: int) -> bool:
    return counter_value < limit


def _get_limit(config: dict[str, Any], key: str, default: int = 3) -> int:
    return int(config.get("retry_limits", {}).get(key, default))


# ---------------------------------------------------------------------------
# Cross-cutting routes (can fire in any phase)
# ---------------------------------------------------------------------------

def route_malformed(
    counters: RetryCounters,
    config: dict[str, Any],
    agent_id: str,
) -> RoutingOutcome:
    """Structural validation failure — re-prompt the same agent or escalate."""
    limit = _get_limit(config, "malformed_output_retries", 2)
    if _within_budget(counters.malformed_output, limit):
        return RoutingOutcome(
            row_id="malformed_output",
            decision="re_prompt_same_agent",
            next_state=f"{agent_id}_reprompt",
            counter_deltas={"malformed_output": 1},
        )
    return RoutingOutcome(
        row_id="malformed_output_exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"malformed_output": 1},
        escalation_reason="malformed_output",
    )


def route_block_flag() -> RoutingOutcome:
    """Block flag present in agent output — immediate halt."""
    return RoutingOutcome(
        row_id="block_flag",
        decision="escalate",
        next_state="failed_escalated",
        escalation_reason="block_flag",
    )


def route_ceiling_exceeded() -> RoutingOutcome:
    """agent_call_count >= max_agent_calls_per_run — global ceiling hit."""
    return RoutingOutcome(
        row_id="global_ceiling_exceeded",
        decision="escalate",
        next_state="failed_escalated",
        escalation_reason="global_ceiling_exceeded",
    )


def route_low_confidence(agent_id: str) -> RoutingOutcome:
    """Policy stage: confidence below threshold."""
    return RoutingOutcome(
        row_id="low_confidence",
        decision="escalate",
        next_state="failed_escalated",
        escalation_reason="low_confidence",
        extra={"agent_id": agent_id},
    )


# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------

def route_requirements_clarify() -> RoutingOutcome:
    """needs_clarification — re-invoke analyst after human answers."""
    return RoutingOutcome(
        row_id="requirements_needs_clarification",
        decision="await_human",
        next_state="requirements_clarification",
    )


def route_requirements_complete() -> RoutingOutcome:
    """status complete — human confirm gate."""
    return RoutingOutcome(
        row_id="requirements_complete_awaiting_confirm",
        decision="await_human",
        next_state="requirements_confirm",
    )


def route_requirements_confirmed() -> RoutingOutcome:
    """Requirements confirmed by human: advance to architecture."""
    return RoutingOutcome(
        row_id="requirements_confirmed",
        decision="invoke_agent",
        next_state="architecture",
    )


def route_requirements_rejected() -> RoutingOutcome:
    """Requirements rejected by human: re-invoke analyst with rejection feedback."""
    return RoutingOutcome(
        row_id="requirements_rejected",
        decision="retry_same_agent",
        next_state="requirements_clarification",
    )


def route_requirements_lowconf() -> RoutingOutcome:
    """Requirements confidence below threshold."""
    return RoutingOutcome(
        row_id="requirements_low_confidence",
        decision="escalate",
        next_state="failed_escalated",
        escalation_reason="low_confidence",
    )


# ---------------------------------------------------------------------------
# Architecture
# ---------------------------------------------------------------------------

def route_architecture_valid(has_locked_decisions: bool) -> RoutingOutcome:
    """Architecture output passes validation (with or without locked tech decisions)."""
    if has_locked_decisions:
        return RoutingOutcome(
            row_id="architecture_locked_awaiting_confirm",
            decision="await_human",
            next_state="tech_decision_confirm",
        )
    return RoutingOutcome(
        row_id="architecture_valid",
        decision="invoke_agent",
        next_state="coding",
    )


def route_architecture_invalid(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """arch_criteria_coverage or other validation fails — re-prompt or escalate."""
    limit = _get_limit(config, "architecture_validation_retries", 2)
    if _within_budget(counters.architecture_validation, limit):
        return RoutingOutcome(
            row_id="architecture_invalid",
            decision="re_prompt_same_agent",
            next_state="architecture",
            counter_deltas={"architecture_validation": 1},
        )
    return RoutingOutcome(
        row_id="architecture_invalid_exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"architecture_validation": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_architecture_lowconf() -> RoutingOutcome:
    """Architecture confidence below threshold."""
    return RoutingOutcome(
        row_id="architecture_low_confidence",
        decision="escalate",
        next_state="failed_escalated",
        escalation_reason="low_confidence",
    )


# ---------------------------------------------------------------------------
# Coding (Implementation)
# ---------------------------------------------------------------------------

def route_coding_no_requirements_txt(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """requirements.txt absent from coder output — re-prompt or escalate."""
    limit = _get_limit(config, "coder_validation_retries", 2)
    if _within_budget(counters.coder_validation, limit):
        return RoutingOutcome(
            row_id="coding_missing_requirements_txt",
            decision="re_prompt_same_agent",
            next_state="coding",
            counter_deltas={"coder_validation": 1},
        )
    return RoutingOutcome(
        row_id="coding_missing_requirements_txt_exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"coder_validation": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_coding_ac_gap(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """Must-have acceptance criteria not covered by code — re-prompt or escalate."""
    limit = _get_limit(config, "coder_validation_retries", 2)
    if _within_budget(counters.coder_validation, limit):
        return RoutingOutcome(
            row_id="coding_acceptance_criteria_gap",
            decision="re_prompt_same_agent",
            next_state="coding",
            counter_deltas={"coder_validation": 1},
        )
    return RoutingOutcome(
        row_id="coding_acceptance_criteria_gap_exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"coder_validation": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_coding_valid() -> RoutingOutcome:
    """Coder output passes all gates — advance to code review."""
    return RoutingOutcome(
        row_id="coding_valid",
        decision="invoke_agent",
        next_state="code_review",
    )


def route_coding_lowconf() -> RoutingOutcome:
    """Coder confidence below threshold."""
    return RoutingOutcome(
        row_id="coding_low_confidence",
        decision="escalate",
        next_state="failed_escalated",
        escalation_reason="low_confidence",
    )


# ---------------------------------------------------------------------------
# Code review
# ---------------------------------------------------------------------------

def route_code_review_fail(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """Code review verdict fail — route back to coder or escalate."""
    limit = _get_limit(config, "code_review_loop", 3)
    if _within_budget(counters.code_review_loop, limit):
        return RoutingOutcome(
            row_id="code_review_fail",
            decision="retry_same_agent",
            next_state="coding",
            counter_deltas={"code_review_loop": 1},
        )
    return RoutingOutcome(
        row_id="code_review_exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"code_review_loop": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_code_review_pass(has_notes: bool) -> RoutingOutcome:
    """Code review passes (optionally with notes) — advance to security review."""
    return RoutingOutcome(
        row_id="code_review_pass_with_notes" if has_notes else "code_review_pass",
        decision="invoke_agent",
        next_state="security_review",
    )


# ---------------------------------------------------------------------------
# Security review
# ---------------------------------------------------------------------------

def route_security_review_fail(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """Security review verdict fail — back to coder (full code review again) or escalate."""
    limit = _get_limit(config, "security_review_loop", 3)
    if _within_budget(counters.security_review_loop, limit):
        return RoutingOutcome(
            row_id="security_review_fail",
            decision="retry_same_agent",
            next_state="coding",
            counter_deltas={"security_review_loop": 1},
        )
    return RoutingOutcome(
        row_id="security_review_exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"security_review_loop": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_security_review_pass(has_notes: bool) -> RoutingOutcome:
    """Security review passes (optionally with notes) — advance to test design."""
    return RoutingOutcome(
        row_id="security_review_pass_with_notes" if has_notes else "security_review_pass",
        decision="invoke_agent",
        next_state="test_design",
    )


# ---------------------------------------------------------------------------
# Test design
# ---------------------------------------------------------------------------

def route_test_design_covmap_invalid(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """Coverage map AC ids don't match requirements_doc — re-prompt or escalate."""
    limit = _get_limit(config, "test_loop", 2)
    if _within_budget(counters.test_loop, limit):
        return RoutingOutcome(
            row_id="test_design_coverage_map_invalid",
            decision="re_prompt_same_agent",
            next_state="test_design",
            counter_deltas={"test_loop": 1},
        )
    return RoutingOutcome(
        row_id="test_design_exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"test_loop": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_test_design_valid() -> RoutingOutcome:
    """Test suite passes validation — advance to test execution."""
    return RoutingOutcome(
        row_id="test_design_valid",
        decision="invoke_agent",
        next_state="test_execution",
    )


# ---------------------------------------------------------------------------
# Test execution (test runner)
# ---------------------------------------------------------------------------

def route_test_execution_error(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """Test runner infrastructure failure — retry the runner or escalate."""
    limit = _get_limit(config, "infrastructure_retries", 3)
    if _within_budget(counters.infrastructure, limit):
        return RoutingOutcome(
            row_id="test_execution_infrastructure_error",
            decision="retry_same_agent",
            next_state="test_execution",
            counter_deltas={"infrastructure": 1},
        )
    return RoutingOutcome(
        row_id="test_execution_infrastructure_error_exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"infrastructure": 1},
        escalation_reason="human_required",
    )


# ---------------------------------------------------------------------------
# Test analysis
# ---------------------------------------------------------------------------

def route_test_analysis_pass() -> RoutingOutcome:
    """Test analyst verdict pass — advance to commit."""
    return RoutingOutcome(
        row_id="test_analysis_pass",
        decision="invoke_agent",
        next_state="commit",
    )


def route_test_analysis_code_bug(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """Test analyst fail_code_bug — back to coder or escalate."""
    limit = _get_limit(config, "test_loop", 2)
    if _within_budget(counters.test_loop, limit):
        return RoutingOutcome(
            row_id="test_analysis_code_bug",
            decision="retry_same_agent",
            next_state="coding",
            counter_deltas={"test_loop": 1},
        )
    return RoutingOutcome(
        row_id="test_analysis_code_bug_exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"test_loop": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_test_analysis_test_bug(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """Test analyst fail_test_bug — back to test designer or escalate."""
    limit = _get_limit(config, "test_loop", 2)
    if _within_budget(counters.test_loop, limit):
        return RoutingOutcome(
            row_id="test_analysis_test_bug",
            decision="retry_same_agent",
            next_state="test_design",
            counter_deltas={"test_loop": 1},
        )
    return RoutingOutcome(
        row_id="test_analysis_test_bug_exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"test_loop": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_test_analysis_spec_gap(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """Test analyst fail_spec_gap — back to architecture designer or escalate."""
    limit = _get_limit(config, "test_loop", 2)
    if _within_budget(counters.test_loop, limit):
        return RoutingOutcome(
            row_id="test_analysis_spec_gap",
            decision="retry_same_agent",
            next_state="architecture",
            counter_deltas={"test_loop": 1},
            # spec_gap rule: also reset review loops
            counter_resets=["code_review_loop", "security_review_loop"],
        )
    return RoutingOutcome(
        row_id="test_analysis_spec_gap_exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"test_loop": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_test_analysis_ambiguous() -> RoutingOutcome:
    """Test analyst fail_ambiguous — escalate to human."""
    return RoutingOutcome(
        row_id="test_analysis_ambiguous",
        decision="escalate",
        next_state="failed_escalated",
        escalation_reason="human_required",
    )


def route_test_analysis_error(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """Test analyst verdict error — re-trigger test runner or escalate."""
    limit = _get_limit(config, "infrastructure_retries", 3)
    if _within_budget(counters.infrastructure, limit):
        return RoutingOutcome(
            row_id="test_analysis_runner_error",
            decision="retry_same_agent",
            next_state="test_execution",
            counter_deltas={"infrastructure": 1},
        )
    return RoutingOutcome(
        row_id="test_analysis_runner_error_exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"infrastructure": 1},
        escalation_reason="human_required",
    )


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------

def route_commit_state_fail(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """Codeforge state commit fails — retry or escalate."""
    limit = _get_limit(config, "codeforge_state_commit", 3)
    if _within_budget(counters.codeforge_state_commit, limit):
        return RoutingOutcome(
            row_id="commit_state_fail",
            decision="retry_same_agent",
            next_state="commit",
            counter_deltas={"codeforge_state_commit": 1},
        )
    return RoutingOutcome(
        row_id="commit_state_fail_exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"codeforge_state_commit": 1},
        escalation_reason="commit_failure",
    )


def route_commit_src_fail(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """Source code commit fails — retry or escalate."""
    limit = _get_limit(config, "source_code_commit", 3)
    if _within_budget(counters.source_code_commit, limit):
        return RoutingOutcome(
            row_id="commit_source_fail",
            decision="retry_same_agent",
            next_state="commit",
            counter_deltas={"source_code_commit": 1},
        )
    return RoutingOutcome(
        row_id="commit_source_fail_exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"source_code_commit": 1},
        escalation_reason="commit_failure",
    )


def route_commit_success() -> RoutingOutcome:
    """Both commits landed — codeforge run succeeded."""
    return RoutingOutcome(
        row_id="commit_success",
        decision="succeed",
        next_state="succeeded",
    )


# ---------------------------------------------------------------------------
# Apply a RoutingOutcome's counter changes to RetryCounters
# ---------------------------------------------------------------------------

def apply_outcome(counters: RetryCounters, outcome: RoutingOutcome) -> RetryCounters:
    """
    Return a new RetryCounters with deltas applied and resets zeroed.
    Does not mutate the input.
    """
    data = counters.model_dump()
    for field, delta in outcome.counter_deltas.items():
        if field in data:
            data[field] = data[field] + delta
    for field in outcome.counter_resets:
        if field in data:
            data[field] = 0
    return RetryCounters(**data)
