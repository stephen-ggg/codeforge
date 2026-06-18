"""
cli/interaction.py — Human interaction layer for the CLI.

All stdout/stdin I/O lives here. The state machine takes `human_interface: Any`
and calls these methods; the CLI passes a HumanInteraction instance.

Methods:
  ask_clarification    — questions + answers loop for Phase 1
  confirm_requirements — display summary, prompt y/N
  get_rejection_feedback — free-text reason when requirements are rejected
  confirm_tech_decisions — display locked decisions, prompt y/N for Phase 2
  handle_escalation    — display escalation event, collect EscalationResolution

All display is to stdout. All input from stdin. No rich/color — plain text only
(the MVP does not include a dashboard UI).
"""

from __future__ import annotations

from typing import Any, Literal, cast

from codeforge.schemas.contracts import (
    ClarificationAnswer,
    ClarificationQuestion,
    EscalationEvent,
    EscalationResolution,
    ReentryDirective,
    ReentryState,
    RequirementsDoc,
    RetryCounters,
)

# Valid reentry states per escalation reason (informational — shown to operator)
_REENTRY_BY_REASON: dict[str, list[str]] = {
    "max_retries_exceeded": [
        "requirements_clarification",
        "architecture",
        "coding",
        "code_review",
        "test_design",
        "test_execution",
        "commit",
    ],
    "block_flag": ["requirements_clarification", "architecture", "coding"],
    "low_confidence": ["requirements_clarification", "architecture", "coding"],
    "global_ceiling_exceeded": [],
    "malformed_output": ["requirements_clarification"],
    "output_truncated": [
        "requirements_clarification",
        "architecture",
        "coding",
        "code_review",
        "test_design",
        "test_execution",
        "commit",
    ],
    "commit_failure": ["commit"],
    "human_required": [
        "requirements_clarification",
        "architecture",
        "coding",
        "test_design",
        "test_execution",
    ],
    "schema_version_mismatch": ["requirements_clarification"],
}

_SEPARATOR = "-" * 60


class HumanInteraction:
    """Handles all terminal I/O between codeforge and the operator."""

    # ------------------------------------------------------------------
    # Phase 1 — Clarification
    # ------------------------------------------------------------------

    def ask_clarification(
        self, questions: list[ClarificationQuestion] | list[dict[str, Any]]
    ) -> list[ClarificationAnswer]:
        """Present clarification questions and collect answers."""
        print(f"\n{_SEPARATOR}")
        print("CLARIFICATION NEEDED")
        print(_SEPARATOR)

        answers: list[ClarificationAnswer] = []
        for i, raw_q in enumerate(questions, 1):
            q = _as_clarification_question(raw_q)
            print(f"\n[{i}/{len(questions)}] {q.question}")
            if q.why_blocking:
                print(f"    Why this matters: {q.why_blocking}")
            if q.options:
                print("    Options:")
                for j, opt in enumerate(q.options, 1):
                    print(f"      {j}. {opt}")

            raw_answer = input("    Your answer: ").strip()

            # If options are given and the user typed a number, resolve to the option text
            selected_option: str | None = None
            if q.options:
                try:
                    idx = int(raw_answer) - 1
                    if 0 <= idx < len(q.options):
                        selected_option = q.options[idx]
                        raw_answer = selected_option
                except ValueError:
                    pass

            answers.append(
                ClarificationAnswer(
                    question_id=q.id,
                    answer=raw_answer,
                    selected_option=selected_option,
                )
            )

        return answers

    # ------------------------------------------------------------------
    # Phase 1 — Requirements confirmation
    # ------------------------------------------------------------------

    def confirm_requirements(
        self, requirements_doc: RequirementsDoc | dict[str, Any]
    ) -> bool:
        """Display the requirements document and ask the operator to confirm."""
        doc = (
            requirements_doc
            if isinstance(requirements_doc, dict)
            else requirements_doc.model_dump()
        )

        print(f"\n{_SEPARATOR}")
        print("REQUIREMENTS DOCUMENT — PLEASE REVIEW")
        print(_SEPARATOR)
        print(f"Feature:     {doc.get('feature_title', '')}")
        print(f"Description: {doc.get('feature_description', '')}")

        scope = doc.get("scope", {})
        if scope.get("in_scope"):
            print("\nIn scope:")
            for item in scope["in_scope"]:
                print(f"  + {item}")
        if scope.get("explicitly_out_of_scope"):
            print("Out of scope:")
            for item in scope["explicitly_out_of_scope"]:
                print(f"  - {item}")

        acs = doc.get("acceptance_criteria", [])
        if acs:
            print("\nAcceptance criteria:")
            for ac in acs:
                pid = ac.get("priority", "")
                print(
                    f"  [{pid.upper():6}] {ac.get('id', '')}: {ac.get('description', '')}"
                )

        print()
        response = input("Confirm? [y/N]: ").strip().lower()
        return response in ("y", "yes")

    def get_rejection_feedback(self) -> str:
        """Collect free-text feedback when the operator rejects the requirements."""
        return input("Feedback — what needs to change? ").strip()

    # ------------------------------------------------------------------
    # Phase 2 — Tech decision confirmation
    # ------------------------------------------------------------------

    def confirm_tech_decisions(
        self, locked_decisions: list[Any]
    ) -> bool:
        """Display locked tech decisions and ask the operator to confirm."""
        print(f"\n{_SEPARATOR}")
        print("LOCKED TECHNOLOGY DECISIONS — PLEASE CONFIRM")
        print(_SEPARATOR)
        for d in locked_decisions:
            if hasattr(d, "id"):
                did, decision, rationale = d.id, d.decision, d.rationale
            else:
                did = d.get("id", "")
                decision = d.get("decision", "")
                rationale = d.get("rationale", "")
            print(f"\n  {did}: {decision}")
            print(f"    Rationale: {rationale}")

        print()
        response = input("Confirm these technology decisions? [y/N]: ").strip().lower()
        return response in ("y", "yes")

    # ------------------------------------------------------------------
    # Escalation handling
    # ------------------------------------------------------------------

    def handle_escalation(self, event: EscalationEvent) -> EscalationResolution:
        """
        Present an escalation event to the operator and collect their resolution.

        Returns an EscalationResolution the CLI can attach to the EscalationEvent.
        """
        print(f"\n{'=' * 60}")
        print("*** CODEFORGE ESCALATION ***")
        print(f"{'=' * 60}")
        print(f"Reason:  {event.reason}")
        print(f"Context: {event.agent_output_ref}")
        print(f"Time:    {event.triggered_at}")

        suggestion = getattr(event, "suggested_reentry_state", None)
        if suggestion:
            print(f"Failed during: {suggestion}")

        reentry_options = _REENTRY_BY_REASON.get(event.reason, [])

        print("\nOptions:")
        print("  1. Approve  — continue with human-directed reentry")
        print("  2. Reject   — abort the codeforge run")
        print("  3. Modify   — provide explicit change instructions before reentry")

        while True:
            choice = input("\nChoice [1/2/3]: ").strip()
            if choice in ("1", "2", "3"):
                break
            print("Please enter 1, 2, or 3.")

        if choice == "2":
            notes = input("Notes (reason for rejection): ").strip()
            return EscalationResolution(outcome="rejected", human_notes=notes)

        reentry_state = _prompt_reentry_state(reentry_options, default=suggestion)

        counter_resets = _prompt_counter_resets()

        if choice == "3":
            change_summary = input("Describe the change you are making: ").strip()
            notes = input("Additional notes: ").strip()
            return EscalationResolution(
                outcome="modified",
                change_summary=change_summary,
                reentry_directive=ReentryDirective(
                    reentry_state=cast(ReentryState, reentry_state),
                    counter_resets=counter_resets,
                    reset_global_ceiling=False,
                ),
                human_notes=notes,
            )

        # choice == "1" — approve
        notes = input("Notes (optional): ").strip()
        return EscalationResolution(
            outcome="approved",
            reentry_directive=ReentryDirective(
                reentry_state=cast(ReentryState, reentry_state),
                counter_resets=counter_resets,
                reset_global_ceiling=False,
            ),
            human_notes=notes,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_clarification_question(raw: Any) -> ClarificationQuestion:
    if isinstance(raw, ClarificationQuestion):
        return raw
    return ClarificationQuestion(**raw)


def _prompt_counter_resets() -> list[str]:
    """Prompt the operator for retry counters to zero on reentry.

    A loop that exhausted its budget (e.g. ``infrastructure``) will re-escalate
    immediately on resume unless its counter is reset. Accepts a comma-separated
    list of counter names, or Enter for none. Invalid names are re-prompted.
    """
    valid = list(RetryCounters.model_fields)

    print("\nReset retry counters? (zeroes a loop's budget so reentry can retry)")
    print(f"  Available: {', '.join(valid)}")
    prompt = "Counters to reset (comma-separated, Enter = none): "

    while True:
        raw = input(prompt).strip()
        if raw == "":
            return []
        names = [n.strip() for n in raw.split(",") if n.strip()]
        invalid = [n for n in names if n not in valid]
        if invalid:
            print(f"Unknown counter(s): {', '.join(invalid)}. Choose from the list above.")
            continue
        # De-duplicate while preserving order.
        return list(dict.fromkeys(names))


def _prompt_reentry_state(options: list[str], default: str | None = None) -> str:
    """Prompt the operator to choose a reentry state, with numbered options and an
    optional default (press Enter to accept)."""
    if not options:
        print("\nNo automatic reentry available. Defaulting to requirements_clarification.")
        return "requirements_clarification"

    print("\nAvailable reentry states:")
    for i, opt in enumerate(options, 1):
        marker = " ← suggested" if opt == default else ""
        print(f"  {i}. {opt}{marker}")

    prompt = f"Reentry state [1-{len(options)}]"
    if default and default in options:
        prompt += f" (Enter = {default})"
    prompt += ": "

    while True:
        raw = input(prompt).strip()
        if raw == "" and default and default in options:
            return default
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx]
        except ValueError:
            if raw in options:
                return raw
        print(f"Please enter a number 1–{len(options)} or press Enter for the default.")
