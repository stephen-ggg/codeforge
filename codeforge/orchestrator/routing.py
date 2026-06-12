"""
orchestrator/routing.py — Codeforge routing table as code.

Each function corresponds to a named row in the spec Part 3 routing table.
No routing logic lives in the state machine — all verdict-to-action mapping is here.

Each handler returns a RoutingOutcome describing:
  - The RoutingDecision (what the orchestrator should do next)
  - Counter deltas (which counters to increment)
  - Counter resets (which counters to zero)
  - next_state: the state label for the event log
  - row_id: the stable routing table row identifier
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
# Cross-cutting routes
# ---------------------------------------------------------------------------

def route_malformed(
    counters: RetryCounters,
    config: dict[str, Any],
    agent_id: str,
) -> RoutingOutcome:
    """X-malformed: Layer 1 structural validation failure."""
    limit = _get_limit(config, "malformed_output_retries", 2)
    if _within_budget(counters.malformed_output, limit):
        return RoutingOutcome(
            row_id="X-malformed",
            decision="re_prompt_same_agent",
            next_state=f"{agent_id}_reprompt",
            counter_deltas={"malformed_output": 1},
        )
    return RoutingOutcome(
        row_id="X-malformed",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"malformed_output": 1},
        escalation_reason="malformed_output",
    )


def route_block_flag() -> RoutingOutcome:
    """X-block: block flag present — immediate halt."""
    return RoutingOutcome(
        row_id="X-block",
        decision="escalate",
        next_state="failed_escalated",
        escalation_reason="block_flag",
    )


def route_ceiling_exceeded() -> RoutingOutcome:
    """X-ceiling: agent_call_count >= max_agent_calls_per_run."""
    return RoutingOutcome(
        row_id="X-ceiling",
        decision="escalate",
        next_state="failed_escalated",
        escalation_reason="global_ceiling_exceeded",
    )


def route_low_confidence(agent_id: str) -> RoutingOutcome:
    """Layer 3: confidence below threshold."""
    return RoutingOutcome(
        row_id="X-lowconf",
        decision="escalate",
        next_state="failed_escalated",
        escalation_reason="low_confidence",
        extra={"agent_id": agent_id},
    )


# ---------------------------------------------------------------------------
# Phase 1 — Requirements
# ---------------------------------------------------------------------------

def route_p1_clarify() -> RoutingOutcome:
    """P1-clarify: needs_clarification — re-invoke analyst after human answers."""
    return RoutingOutcome(
        row_id="P1-clarify",
        decision="await_human",
        next_state="requirements_clarification",
    )


def route_p1_complete() -> RoutingOutcome:
    """P1-complete: status complete — human confirm gate."""
    return RoutingOutcome(
        row_id="P1-complete",
        decision="await_human",
        next_state="requirements_confirm",
    )


def route_p1_confirmed() -> RoutingOutcome:
    """P1 confirmed: advance to Phase 2."""
    return RoutingOutcome(
        row_id="P1-complete",
        decision="invoke_agent",
        next_state="architecture",
    )


def route_p1_rejected() -> RoutingOutcome:
    """P1 rejected: re-invoke requirements analyst with rejection feedback."""
    return RoutingOutcome(
        row_id="P1-complete",
        decision="retry_same_agent",
        next_state="requirements_clarification",
    )


def route_p1_lowconf() -> RoutingOutcome:
    """P1-lowconf: confidence below threshold."""
    return RoutingOutcome(
        row_id="P1-lowconf",
        decision="escalate",
        next_state="failed_escalated",
        escalation_reason="low_confidence",
    )


# ---------------------------------------------------------------------------
# Phase 2 — Architecture
# ---------------------------------------------------------------------------

def route_p2_valid(has_locked_decisions: bool) -> RoutingOutcome:
    """P2-valid or P2-locked: architecture output passes validation."""
    if has_locked_decisions:
        return RoutingOutcome(
            row_id="P2-locked",
            decision="await_human",
            next_state="tech_decision_confirm",
        )
    return RoutingOutcome(
        row_id="P2-valid",
        decision="invoke_agent",
        next_state="coding",
    )


def route_p2_invalid(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """P2-invalid: arch_criteria_coverage or other validation fails."""
    limit = _get_limit(config, "architecture_validation_retries", 2)
    if _within_budget(counters.architecture_validation, limit):
        return RoutingOutcome(
            row_id="P2-invalid",
            decision="re_prompt_same_agent",
            next_state="architecture",
            counter_deltas={"architecture_validation": 1},
        )
    return RoutingOutcome(
        row_id="P2-invalid",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"architecture_validation": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_p2_lowconf() -> RoutingOutcome:
    """P2-lowconf."""
    return RoutingOutcome(
        row_id="P2-lowconf",
        decision="escalate",
        next_state="failed_escalated",
        escalation_reason="low_confidence",
    )


# ---------------------------------------------------------------------------
# Phase 3 — Implementation (Coder)
# ---------------------------------------------------------------------------

def route_p3_no_requirements_txt(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """P3-no-req: requirements.txt absent."""
    limit = _get_limit(config, "coder_validation_retries", 2)
    if _within_budget(counters.coder_validation, limit):
        return RoutingOutcome(
            row_id="P3-no-req",
            decision="re_prompt_same_agent",
            next_state="coding",
            counter_deltas={"coder_validation": 1},
        )
    return RoutingOutcome(
        row_id="P3-no-req",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"coder_validation": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_p3_ac_gap(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """P3-ac-gap: must ACs not covered."""
    limit = _get_limit(config, "coder_validation_retries", 2)
    if _within_budget(counters.coder_validation, limit):
        return RoutingOutcome(
            row_id="P3-ac-gap",
            decision="re_prompt_same_agent",
            next_state="coding",
            counter_deltas={"coder_validation": 1},
        )
    return RoutingOutcome(
        row_id="P3-ac-gap",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"coder_validation": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_p3_valid() -> RoutingOutcome:
    """P3-valid: coder output passes all gates — advance to Loop A."""
    return RoutingOutcome(
        row_id="P3-valid",
        decision="invoke_agent",
        next_state="code_review",
    )


def route_p3_lowconf() -> RoutingOutcome:
    """P3-lowconf."""
    return RoutingOutcome(
        row_id="P3-lowconf",
        decision="escalate",
        next_state="failed_escalated",
        escalation_reason="low_confidence",
    )


# ---------------------------------------------------------------------------
# Phase 4A — Code review (Loop A)
# ---------------------------------------------------------------------------

def route_p4a_fail(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """P4A-fail: code review verdict fail — route back to coder."""
    limit = _get_limit(config, "code_review_loop", 3)
    if _within_budget(counters.code_review_loop, limit):
        return RoutingOutcome(
            row_id="P4A-fail",
            decision="retry_same_agent",
            next_state="coding",
            counter_deltas={"code_review_loop": 1},
        )
    return RoutingOutcome(
        row_id="P4A-exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"code_review_loop": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_p4a_pass(has_notes: bool) -> RoutingOutcome:
    """P4A-pass or P4A-notes: code review passes — advance to Loop B."""
    return RoutingOutcome(
        row_id="P4A-notes" if has_notes else "P4A-pass",
        decision="invoke_agent",
        next_state="security_review",
    )


# ---------------------------------------------------------------------------
# Phase 4B — Security review (Loop B)
# ---------------------------------------------------------------------------

def route_p4b_fail(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """P4B-fail: security review verdict fail — back to coder, full Loop A again."""
    limit = _get_limit(config, "security_review_loop", 3)
    if _within_budget(counters.security_review_loop, limit):
        return RoutingOutcome(
            row_id="P4B-fail",
            decision="retry_same_agent",
            next_state="coding",
            counter_deltas={"security_review_loop": 1},
        )
    return RoutingOutcome(
        row_id="P4B-exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"security_review_loop": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_p4b_pass(has_notes: bool) -> RoutingOutcome:
    """P4B-pass or P4B-notes: security review passes — advance to Phase 5."""
    return RoutingOutcome(
        row_id="P4B-notes" if has_notes else "P4B-pass",
        decision="invoke_agent",
        next_state="test_design",
    )


# ---------------------------------------------------------------------------
# Phase 5 — Testing (Loop C)
# ---------------------------------------------------------------------------

def route_p5d_covmap_invalid(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """P5D-covmap: coverage map AC ids don't match requirements_doc."""
    limit = _get_limit(config, "test_loop", 2)
    if _within_budget(counters.test_loop, limit):
        return RoutingOutcome(
            row_id="P5D-covmap",
            decision="re_prompt_same_agent",
            next_state="test_design",
            counter_deltas={"test_loop": 1},
        )
    return RoutingOutcome(
        row_id="P5C-exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"test_loop": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_p5e_error(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """P5E-error: test runner infrastructure failure."""
    limit = _get_limit(config, "infrastructure_retries", 3)
    if _within_budget(counters.infrastructure, limit):
        return RoutingOutcome(
            row_id="P5E-error",
            decision="retry_same_agent",
            next_state="test_execution",
            counter_deltas={"infrastructure": 1},
        )
    return RoutingOutcome(
        row_id="P5E-error",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"infrastructure": 1},
        escalation_reason="human_required",
    )


def route_p5c_pass() -> RoutingOutcome:
    """P5C-pass: test analyst verdict pass — advance to Phase 6."""
    return RoutingOutcome(
        row_id="P5C-pass",
        decision="invoke_agent",
        next_state="commit",
    )


def route_p5c_code_bug(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """P5C-code-bug: test analyst fail_code_bug — back to coder."""
    limit = _get_limit(config, "test_loop", 2)
    if _within_budget(counters.test_loop, limit):
        return RoutingOutcome(
            row_id="P5C-code-bug",
            decision="retry_same_agent",
            next_state="coding",
            counter_deltas={"test_loop": 1},
        )
    return RoutingOutcome(
        row_id="P5C-exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"test_loop": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_p5c_test_bug(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """P5C-test-bug: test analyst fail_test_bug — back to test designer."""
    limit = _get_limit(config, "test_loop", 2)
    if _within_budget(counters.test_loop, limit):
        return RoutingOutcome(
            row_id="P5C-test-bug",
            decision="retry_same_agent",
            next_state="test_design",
            counter_deltas={"test_loop": 1},
        )
    return RoutingOutcome(
        row_id="P5C-exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"test_loop": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_p5c_spec_gap(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """P5C-spec-gap: test analyst fail_spec_gap — back to architecture designer."""
    limit = _get_limit(config, "test_loop", 2)
    if _within_budget(counters.test_loop, limit):
        return RoutingOutcome(
            row_id="P5C-spec-gap",
            decision="retry_same_agent",
            next_state="architecture",
            counter_deltas={"test_loop": 1},
            # spec_gap rule: also reset review loops
            counter_resets=["code_review_loop", "security_review_loop"],
        )
    return RoutingOutcome(
        row_id="P5C-exhausted",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"test_loop": 1},
        escalation_reason="max_retries_exceeded",
    )


def route_p5c_ambiguous() -> RoutingOutcome:
    """P5C-ambiguous: test analyst fail_ambiguous — escalate to human."""
    return RoutingOutcome(
        row_id="P5C-ambiguous",
        decision="escalate",
        next_state="failed_escalated",
        escalation_reason="human_required",
    )


def route_p5c_analyst_error(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """P5C-error: test analyst verdict error — re-trigger test runner."""
    limit = _get_limit(config, "infrastructure_retries", 3)
    if _within_budget(counters.infrastructure, limit):
        return RoutingOutcome(
            row_id="P5C-error",
            decision="retry_same_agent",
            next_state="test_execution",
            counter_deltas={"infrastructure": 1},
        )
    return RoutingOutcome(
        row_id="P5C-error",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"infrastructure": 1},
        escalation_reason="human_required",
    )


# ---------------------------------------------------------------------------
# Phase 6 — Commit
# ---------------------------------------------------------------------------

def route_p6_state_fail(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """P6-state-fail: codeforge state commit fails."""
    limit = _get_limit(config, "codeforge_state_commit", 3)
    if _within_budget(counters.codeforge_state_commit, limit):
        return RoutingOutcome(
            row_id="P6-state-fail",
            decision="retry_same_agent",
            next_state="commit",
            counter_deltas={"codeforge_state_commit": 1},
        )
    return RoutingOutcome(
        row_id="P6-state-fail",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"codeforge_state_commit": 1},
        escalation_reason="commit_failure",
    )


def route_p6_src_fail(
    counters: RetryCounters,
    config: dict[str, Any],
) -> RoutingOutcome:
    """P6-src-fail: source code commit fails."""
    limit = _get_limit(config, "source_code_commit", 3)
    if _within_budget(counters.source_code_commit, limit):
        return RoutingOutcome(
            row_id="P6-src-fail",
            decision="retry_same_agent",
            next_state="commit",
            counter_deltas={"source_code_commit": 1},
        )
    return RoutingOutcome(
        row_id="P6-src-fail",
        decision="escalate",
        next_state="failed_escalated",
        counter_deltas={"source_code_commit": 1},
        escalation_reason="commit_failure",
    )


def route_p6_success() -> RoutingOutcome:
    """P6-src-ok: both commits landed — codeforge run succeeded."""
    return RoutingOutcome(
        row_id="P6-src-ok",
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
