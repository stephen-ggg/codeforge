"""Tests for the counter-reset prompt in the escalation resolution flow.

See codeforge/cli/interaction.py — handle_escalation() now lets the operator
zero retry counters (e.g. ``infrastructure``) so a budget-exhausted loop can
actually retry on reentry instead of re-escalating immediately.
"""

from __future__ import annotations

from typing import Iterator

import pytest

from codeforge.cli.interaction import HumanInteraction, _prompt_counter_resets
from codeforge.schemas.contracts import EscalationEvent


def _inputs(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> None:
    it: Iterator[str] = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(it))


def test_prompt_counter_resets_empty_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _inputs(monkeypatch, [""])
    assert _prompt_counter_resets() == []


def test_prompt_counter_resets_parses_and_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    _inputs(monkeypatch, [" infrastructure , test_loop , infrastructure "])
    assert _prompt_counter_resets() == ["infrastructure", "test_loop"]


def test_prompt_counter_resets_reprompts_on_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    _inputs(monkeypatch, ["bogus", "infrastructure"])
    assert _prompt_counter_resets() == ["infrastructure"]


def _escalation() -> EscalationEvent:
    return EscalationEvent(
        escalation_id="esc-1",
        triggered_at="2026-06-15T00:00:00+00:00",
        reason="max_retries_exceeded",
        agent_output_ref="artifact-1",
        suggested_reentry_state="test_execution",
        resolved=False,
    )


def test_handle_escalation_approve_collects_counter_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # choice=1 (approve), reentry=Enter (suggested), counters, notes.
    _inputs(monkeypatch, ["1", "", "infrastructure", "notes"])
    resolution = HumanInteraction().handle_escalation(_escalation())

    assert resolution.outcome == "approved"
    assert resolution.reentry_directive is not None
    assert resolution.reentry_directive.reentry_state == "test_execution"
    assert resolution.reentry_directive.counter_resets == ["infrastructure"]


def test_handle_escalation_reject_skips_counter_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Rejection returns before any reentry/counter prompt — only the notes input.
    _inputs(monkeypatch, ["2", "not viable"])
    resolution = HumanInteraction().handle_escalation(_escalation())

    assert resolution.outcome == "rejected"
    assert resolution.reentry_directive is None
