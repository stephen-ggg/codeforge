"""
store/renderers/ui_design.py — Deterministic markdown render for ui_design.json.

Pure function. No LLM. No parsing of the output — only generation.
Component order is preserved from the JSON array (never sorted).
"""

from __future__ import annotations

from codeforge.schemas.contracts import UIDesignState

_STATUS_BADGE = {
    "not_started": "[NOT STARTED]",
    "in_progress": "[IN PROGRESS]",
    "built": "[BUILT]",
}


def render_ui_design(state: UIDesignState) -> str:
    lines: list[str] = []

    lines.append("# UI Design")
    lines.append("")
    lines.append(f"**Design source:** {state.design_source}  ")
    lines.append(f"**Last updated:** {state.last_updated_run}")
    lines.append("")

    lines.append("## Design Tokens")
    lines.append("")
    if not state.design_tokens:
        lines.append("_No design tokens defined._")
    else:
        lines.append("| Name | Value | Usage |")
        lines.append("|------|-------|-------|")
        for token in state.design_tokens:
            lines.append(f"| {token.name} | {token.value} | {token.usage} |")
    lines.append("")

    lines.append("## Phase Colors")
    lines.append("")
    if not state.phase_colors:
        lines.append("_No phase colors defined._")
    else:
        lines.append("| Phase | Color |")
        lines.append("|-------|-------|")
        for pc in state.phase_colors:
            lines.append(f"| {pc.phase_id} | {pc.color} |")
    lines.append("")

    lines.append("## Font Family")
    lines.append("")
    lines.append(state.font_family)
    lines.append("")

    lines.append("## Components")
    lines.append("")
    if not state.components:
        lines.append("_No components defined._")
    else:
        for comp in state.components:
            badge = _STATUS_BADGE.get(comp.status, comp.status.upper())
            lines.append(f"### {comp.id} — {badge}")
            lines.append(comp.description)
            lines.append("")
            lines.append(f"**Props:** {', '.join(comp.props) if comp.props else '—'}")
            lines.append(f"**Data dependencies:** {', '.join(comp.data_dependencies) if comp.data_dependencies else '—'}")
            if comp.interactions:
                lines.append("**Interactions:**")
                for interaction in comp.interactions:
                    lines.append(f"- {interaction}")
            else:
                lines.append("**Interactions:** —")
            lines.append(f"**Notes:** {comp.notes}")
            lines.append("")
            lines.append("---")
            lines.append("")

    return "\n".join(lines)
