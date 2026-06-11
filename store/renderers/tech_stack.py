"""
store/renderers/tech_stack.py — Deterministic markdown render for tech_stack.json.

Pure function. No LLM. No parsing of the output — only generation.
"""

from __future__ import annotations

from codeforge.schemas.contracts import TechStackState


def render_tech_stack(state: TechStackState) -> str:
    lines: list[str] = []

    lines.append("# Tech Stack")
    lines.append("")
    lines.append(f"**Schema version:** {state.schema_version}")
    lines.append("")

    if not state.decisions:
        lines.append("_No tech decisions recorded._")
        lines.append("")
        return "\n".join(lines)

    # Locked decisions first, then unlocked
    locked = [d for d in state.decisions if d.get("locked")]
    unlocked = [d for d in state.decisions if not d.get("locked")]

    if locked:
        lines.append("## Locked Decisions")
        lines.append("")
        lines.append("_These decisions are immutable and require human override to change._")
        lines.append("")
        for decision in locked:
            _render_decision(lines, decision)

    if unlocked:
        lines.append("## Recorded Decisions")
        lines.append("")
        for decision in unlocked:
            _render_decision(lines, decision)

    return "\n".join(lines)


def _render_decision(lines: list[str], decision: dict) -> None:  # type: ignore[type-arg]
    decision_id = decision.get("id", "unknown")
    domain = decision.get("domain", "")
    text = decision.get("decision", "")
    rationale = decision.get("rationale", "")
    run_id = decision.get("run_id", "")
    confirmed_at = decision.get("confirmed_at", "")
    supersedes = decision.get("supersedes")

    lines.append(f"### {decision_id}  —  {domain}")
    lines.append("")
    lines.append(f"**Decision:** {text}")
    lines.append("")
    lines.append(f"**Rationale:** {rationale}")
    lines.append("")
    if run_id:
        lines.append(f"**Run:** {run_id}")
    if confirmed_at:
        lines.append(f"**Confirmed at:** {confirmed_at}")
    if supersedes:
        lines.append(f"**Supersedes:** {supersedes}")
    lines.append("")
