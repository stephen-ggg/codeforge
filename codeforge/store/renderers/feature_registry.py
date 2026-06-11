"""
store/renderers/feature_registry.py — Deterministic markdown render for feature_registry.json.

Pure function. No LLM. No parsing of the output — only generation.
"""

from __future__ import annotations

from codeforge.schemas.contracts import FeatureRegistry


def render_feature_registry(state: FeatureRegistry) -> str:
    lines: list[str] = []

    lines.append("# Feature Registry")
    lines.append("")
    lines.append(f"**Schema version:** {state.schema_version}")
    lines.append("")

    if not state.features:
        lines.append("_No features registered._")
        lines.append("")
        return "\n".join(lines)

    status_order = {"implemented": 0, "tested": 1, "deprecated": 2}
    sorted_features = sorted(
        state.features, key=lambda f: status_order.get(f.status, 99)
    )

    for feature in sorted_features:
        status_badge = {
            "implemented": "🔨 implemented",
            "tested": "✅ tested",
            "deprecated": "🚫 deprecated",
        }.get(feature.status, feature.status)

        lines.append(f"## {feature.feature_title}  {status_badge}")
        lines.append("")
        lines.append(f"**Introduced:** {feature.introduced_run}  ")
        lines.append(f"**Last modified:** {feature.last_modified_run}")
        lines.append("")

        stable_interfaces = [
            i for i in feature.interfaces if i.stability == "stable"
        ]
        if stable_interfaces:
            lines.append("**Stable interfaces:**")
            lines.append("")
            for iface in stable_interfaces:
                lines.append(f"- `{iface.name}` ({iface.kind}) — owner: {iface.owner_module}")
            lines.append("")

    return "\n".join(lines)
