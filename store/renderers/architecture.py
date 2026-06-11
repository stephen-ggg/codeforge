"""
store/renderers/architecture.py — Deterministic markdown render for architecture.json.

Pure function. No LLM. No parsing of the output — only generation.
"""

from __future__ import annotations

from codeforge.schemas.contracts import ArchitectureState


def render_architecture(state: ArchitectureState) -> str:
    lines: list[str] = []

    lines.append("# Architecture")
    lines.append("")
    lines.append(f"**Schema version:** {state.schema_version}  ")
    lines.append(f"**Last updated run:** {state.last_updated_run}")
    lines.append("")

    # Modules
    lines.append("## Modules")
    lines.append("")
    if not state.modules:
        lines.append("_No modules defined._")
    else:
        for mod in state.modules:
            lines.append(f"### {mod.name}")
            lines.append("")
            lines.append(f"**Responsibility:** {mod.responsibility}")
            lines.append("")
            if mod.dependencies:
                lines.append(f"**Dependencies:** {', '.join(mod.dependencies)}")
            if mod.exposes:
                lines.append(f"**Exposes:** {', '.join(mod.exposes)}")
            if mod.consumes:
                lines.append(f"**Consumes:** {', '.join(mod.consumes)}")
            lines.append("")

    # Interfaces
    lines.append("## Interfaces")
    lines.append("")
    if not state.interfaces:
        lines.append("_No interfaces defined._")
    else:
        for iface in state.interfaces:
            stability_badge = {
                "stable": "✅ stable",
                "experimental": "⚠️ experimental",
                "deprecated": "🚫 deprecated",
            }.get(iface.stability, iface.stability)
            lines.append(f"### {iface.name}  `{iface.kind}`  {stability_badge}")
            lines.append("")
            lines.append(f"**Owner:** {iface.owner_module}")
            if iface.successor:
                lines.append(f"**Successor:** {iface.successor}")
            if iface.removal_run:
                lines.append(f"**Removal run:** {iface.removal_run}")
            lines.append("")

    # Data flow
    lines.append("## Data Flow")
    lines.append("")
    if not state.data_flow:
        lines.append("_No data flows defined._")
    else:
        for flow in state.data_flow:
            lines.append(f"- **{flow.name}:** `{flow.from_}` → `{flow.to}` via `{flow.via}`")
            lines.append(f"  {flow.data_description}")
    lines.append("")

    return "\n".join(lines)
