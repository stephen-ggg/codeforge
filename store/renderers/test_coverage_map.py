"""
store/renderers/test_coverage_map.py — Deterministic markdown render for test_coverage_map.json.

Pure function. No LLM. No parsing of the output — only generation.
"""

from __future__ import annotations

from codeforge.schemas.contracts import TestCoverageMap


def render_test_coverage_map(state: TestCoverageMap) -> str:
    lines: list[str] = []

    lines.append("# Test Coverage Map")
    lines.append("")
    lines.append(f"**Schema version:** {state.schema_version}")
    lines.append("")

    if not state.entries:
        lines.append("_No coverage recorded._")
        lines.append("")
        return "\n".join(lines)

    # Summary counts
    covered = sum(1 for e in state.entries if e.status == "covered")
    partial = sum(1 for e in state.entries if e.status == "partial")
    not_covered = sum(1 for e in state.entries if e.status == "not_covered")
    total = len(state.entries)

    lines.append(
        f"**Coverage summary:** {covered}/{total} covered · "
        f"{partial} partial · {not_covered} not covered"
    )
    lines.append("")

    # Status order: not_covered first (most urgent), then partial, then covered
    status_order = {"not_covered": 0, "partial": 1, "covered": 2}
    sorted_entries = sorted(
        state.entries, key=lambda e: (status_order.get(e.status, 99), e.criterion_id)
    )

    for entry in sorted_entries:
        status_badge = {
            "covered": "✅ covered",
            "partial": "⚠️ partial",
            "not_covered": "❌ not covered",
        }.get(entry.status, entry.status)

        lines.append(f"## {entry.criterion_id}  —  {status_badge}")
        lines.append("")
        lines.append(f"**Run:** {entry.run_id}")
        if entry.test_case_ids:
            lines.append(f"**Test cases:** {', '.join(f'`{t}`' for t in entry.test_case_ids)}")
        if entry.notes:
            lines.append(f"**Notes:** {entry.notes}")
        lines.append("")

    return "\n".join(lines)
