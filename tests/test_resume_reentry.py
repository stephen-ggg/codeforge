"""Tests for resume reentry selection — see codeforge/cli/commands.py.

These cover the pure decision logic that lets `codeforge resume` re-enter a run
at the operator-chosen reentry state, including the case where a prior resume
already resolved the escalation (e.g. it crashed afterwards). No CLI/LLM stack.
"""

from __future__ import annotations

from codeforge.cli.commands import _initial_state_from, _select_resume_escalation
from codeforge.schemas.contracts import (
    CodeforgeRun,
    EscalationEvent,
    EscalationResolution,
    ReentryDirective,
    RetryCounters,
)


def _run(*escalations: EscalationEvent, status: str = "failed_escalated") -> CodeforgeRun:
    return CodeforgeRun(
        run_id="run-test",
        codeforge_version="codeforge-v1",
        run_mode="new_project",
        started_at="2026-06-15T00:00:00+00:00",
        status=status,  # type: ignore[arg-type]
        config_snapshot={},
        retry_counters=RetryCounters(),
        escalations=list(escalations),
    )


def _escalation(
    *,
    resolved: bool,
    reentry_state: str | None = None,
    outcome: str = "approved",
) -> EscalationEvent:
    resolution = None
    if resolved:
        directive = (
            ReentryDirective(reentry_state=reentry_state, counter_resets=[])  # type: ignore[arg-type]
            if reentry_state is not None
            else None
        )
        resolution = EscalationResolution(
            outcome=outcome,  # type: ignore[arg-type]
            reentry_directive=directive,
            human_notes="",
        )
    return EscalationEvent(
        escalation_id="esc-1",
        triggered_at="2026-06-15T00:00:00+00:00",
        reason="human_required",
        agent_output_ref="artifact-1",
        resolved=resolved,
        resolution=resolution,
    )


def test_no_escalations_returns_none_no_prompt():
    escalation, needs_prompt = _select_resume_escalation(_run())
    assert escalation is None
    assert needs_prompt is False
    assert _initial_state_from(escalation) == "requirements"


def test_unresolved_escalation_prompts():
    esc = _escalation(resolved=False)
    escalation, needs_prompt = _select_resume_escalation(_run(esc))
    assert escalation is esc
    assert needs_prompt is True


def test_resolved_escalation_silent_reentry():
    esc = _escalation(resolved=True, reentry_state="test_execution")
    escalation, needs_prompt = _select_resume_escalation(_run(esc))
    assert escalation is esc
    assert needs_prompt is False
    # The crashed-resume case: re-enter at the stored reentry state, no re-prompt.
    assert _initial_state_from(escalation) == "test_execution"


def test_latest_escalation_wins_over_earlier_resolved():
    earlier = _escalation(resolved=True, reentry_state="coding")
    later = _escalation(resolved=False)
    escalation, needs_prompt = _select_resume_escalation(_run(earlier, later))
    assert escalation is later
    assert needs_prompt is True


def test_resolved_without_directive_defaults_to_requirements():
    esc = _escalation(resolved=True, reentry_state=None)
    escalation, needs_prompt = _select_resume_escalation(_run(esc))
    assert needs_prompt is False
    assert _initial_state_from(escalation) == "requirements"
