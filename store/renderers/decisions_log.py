"""
store/renderers/decisions_log.py — Deterministic markdown render for decisions_log.json.

Pure function. No LLM. No parsing of the output — only generation.
Append-only document: newest entries appear first.
"""

from __future__ import annotations

from codeforge.schemas.contracts import DecisionsLog


def render_decisions_log(state: DecisionsLog) -> str:
    lines: list[str] = []

    lines.append("# Decisions Log")
    lines.append("")
    lines.append(f"**Schema version:** {state.schema_version}")
    lines.append("")

    if not state.entries:
        lines.append("_No decisions recorded._")
        lines.append("")
        return "\n".join(lines)

    # Newest first
    for entry in reversed(state.entries):
        type_badge = {
            "agent_decision": "🤖 agent decision",
            "human_override": "👤 human override",
        }.get(entry.entry_type, entry.entry_type)

        lines.append(f"## {entry.entry_id}  —  {type_badge}")
        lines.append("")
        lines.append(f"**Run:** {entry.run_id}  ")
        lines.append(f"**Created:** {entry.created_at}")
        if entry.source_agent:
            lines.append(f"**Source agent:** {entry.source_agent}")
        lines.append("")
        lines.append(f"**Decision:** {entry.decision}")
        lines.append("")
        lines.append(f"**Rationale:** {entry.rationale}")
        lines.append("")

    return "\n".join(lines)
