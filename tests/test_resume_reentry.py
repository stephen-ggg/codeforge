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


def test_no_escalations_returns_none_no_prompt() -> None:
    escalation, needs_prompt = _select_resume_escalation(_run())
    assert escalation is None
    assert needs_prompt is False
    assert _initial_state_from(escalation) == "requirements"


def test_unresolved_escalation_prompts() -> None:
    esc = _escalation(resolved=False)
    escalation, needs_prompt = _select_resume_escalation(_run(esc))
    assert escalation is esc
    assert needs_prompt is True


def test_resolved_escalation_silent_reentry() -> None:
    esc = _escalation(resolved=True, reentry_state="test_execution")
    escalation, needs_prompt = _select_resume_escalation(_run(esc))
    assert escalation is esc
    assert needs_prompt is False
    # The crashed-resume case: re-enter at the stored reentry state, no re-prompt.
    assert _initial_state_from(escalation) == "test_execution"


def test_latest_escalation_wins_over_earlier_resolved() -> None:
    earlier = _escalation(resolved=True, reentry_state="coding")
    later = _escalation(resolved=False)
    escalation, needs_prompt = _select_resume_escalation(_run(earlier, later))
    assert escalation is later
    assert needs_prompt is True


def test_resolved_without_directive_defaults_to_requirements() -> None:
    esc = _escalation(resolved=True, reentry_state=None)
    escalation, needs_prompt = _select_resume_escalation(_run(esc))
    assert needs_prompt is False
    assert _initial_state_from(escalation) == "requirements"


def test_initial_state_falls_back_to_suggested_reentry() -> None:
    """A resolved escalation with no reentry_directive re-enters at the phase that was
    running (suggested_reentry_state), not a full restart from requirements."""
    esc = EscalationEvent(
        escalation_id="esc-1",
        triggered_at="2026-06-15T00:00:00+00:00",
        reason="human_required",
        agent_output_ref="a1",
        resolved=True,
        resolution=EscalationResolution(
            outcome="approved", reentry_directive=None, human_notes=""
        ),
        suggested_reentry_state="coding",
    )
    assert _initial_state_from(esc) == "coding"


def test_initial_state_directive_overrides_suggested() -> None:
    esc = EscalationEvent(
        escalation_id="esc-1",
        triggered_at="2026-06-15T00:00:00+00:00",
        reason="human_required",
        agent_output_ref="a1",
        resolved=True,
        resolution=EscalationResolution(
            outcome="approved",
            reentry_directive=ReentryDirective(
                reentry_state="test_design", counter_resets=[]
            ),
            human_notes="",
        ),
        suggested_reentry_state="coding",
    )
    assert _initial_state_from(esc) == "test_design"


def test_malformed_output_reentry_allowlist_matches_output_truncated() -> None:
    """malformed_output must allow the full phase chain like output_truncated, so the
    operator can re-enter at the escalation's suggested phase (e.g. test_design) rather
    than being forced into a full-pipeline restart from requirements_clarification."""
    from typing import get_args

    from codeforge.cli.interaction import _REENTRY_BY_REASON
    from codeforge.schemas.contracts import ReentryState

    malformed = _REENTRY_BY_REASON["malformed_output"]
    assert "test_design" in malformed
    # Structurally identical to output_truncated — keep the two in lockstep.
    assert malformed == _REENTRY_BY_REASON["output_truncated"]
    # Every offered option must be a valid ReentryState.
    assert set(malformed) <= set(get_args(ReentryState))


def test_reentry_options_bounded_by_failing_phase() -> None:
    """A run that failed in test_design must never be offered a downstream phase
    (test_execution / commit) — those phases never ran, so their artifacts (the
    test_suite) don't exist. This is the run-097cfe57faf8 case."""
    from codeforge.cli.interaction import reentry_options_for

    opts = reentry_options_for("malformed_output", "test_design")
    assert "test_design" in opts
    assert "test_execution" not in opts
    assert "commit" not in opts
    # Everything at or before the failing phase remains available.
    assert opts == [
        "requirements_clarification",
        "architecture",
        "coding",
        "code_review",
        "test_design",
    ]


def test_reentry_options_unbounded_when_phase_unknown() -> None:
    """A None/unknown failing phase falls back to the full per-reason allowlist."""
    from codeforge.cli.interaction import _REENTRY_BY_REASON, reentry_options_for

    assert reentry_options_for("malformed_output", None) == _REENTRY_BY_REASON["malformed_output"]


def test_reentry_options_allow_commit_when_commit_is_the_failing_phase() -> None:
    """commit_failure failed at commit, so everything completed — commit stays valid."""
    from codeforge.cli.interaction import reentry_options_for

    assert reentry_options_for("commit_failure", "commit") == ["commit"]
