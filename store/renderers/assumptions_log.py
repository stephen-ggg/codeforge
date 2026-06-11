"""
store/renderers/assumptions_log.py — Deterministic markdown render for assumptions_log.json.

Pure function. No LLM. No parsing of the output — only generation.
"""

from __future__ import annotations

from codeforge.schemas.contracts import AssumptionsLog


def render_assumptions_log(state: AssumptionsLog) -> str:
    lines: list[str] = []

    lines.append("# Assumptions Log")
    lines.append("")
    lines.append(f"**Schema version:** {state.schema_version}")
    lines.append("")

    if not state.entries:
        lines.append("_No assumptions recorded._")
        lines.append("")
        return "\n".join(lines)

    # Group by status: open first (most actionable), then resolved, then superseded
    status_order = {"open": 0, "resolved": 1, "superseded": 2}
    sorted_entries = sorted(
        state.entries, key=lambda e: (status_order.get(e.status, 99), e.run_id)
    )

    for entry in sorted_entries:
        status_badge = {
            "open": "🔴 open",
            "resolved": "✅ resolved",
            "superseded": "⚪ superseded",
        }.get(entry.status, entry.status)

        impact_badge = {
            "high": "🔴 high",
            "medium": "🟡 medium",
            "low": "🟢 low",
        }.get(entry.impact, entry.impact)

        lines.append(f"## {entry.id}  —  {status_badge}")
        lines.append("")
        lines.append(f"**Impact:** {impact_badge}  ")
        lines.append(f"**Run:** {entry.run_id}  ")
        lines.append(f"**Source agent:** {entry.source_agent}")
        lines.append("")
        lines.append(f"{entry.description}")
        lines.append("")

    return "\n".join(lines)
