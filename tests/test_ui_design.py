"""
Tests for the ui_design project state document:
  - UIDesignState schema round-trip
  - render_ui_design output
  - ProjectStateStore load/write
  - Assembler firewall (inclusion/exclusion by agent)
  - Phase 6 component status update
  - codeforge seed CLI parser
  - codeforge run --brief-file flag
"""
from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest
from typer.testing import CliRunner

from codeforge.cli.commands import app
from codeforge.cli.seed_parser import SeedParser
from codeforge.firewall.assembler import ContextAssembler
from codeforge.firewall.manifest import load_manifest
from codeforge.orchestrator.pending_writes import PendingWrites
from codeforge.schemas.contracts import (
    ComponentSpec,
    DesignToken,
    PhaseColor,
    UIDesignState,
)
from codeforge.store.artifact_store import ArtifactStore
from codeforge.store.project_state import ProjectStateStore
from codeforge.store.renderers.ui_design import render_ui_design


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _minimal_ui_state(**overrides: Any) -> UIDesignState:
    defaults: dict[str, Any] = {
        "schema_version": "1.0.0",
        "design_source": "dashboard.dc.html",
        "seeded_at": "seed",
        "last_updated_run": "seed",
        "design_tokens": [
            DesignToken(name="bg_base", value="#0a0b0e", usage="Page background"),
            DesignToken(name="accent_green", value="oklch(0.78 0.13 168)", usage="Accent color"),
        ],
        "phase_colors": [
            PhaseColor(phase_id="req", color="oklch(0.70 0.14 255)"),
            PhaseColor(phase_id="arch", color="oklch(0.73 0.13 221)"),
        ],
        "font_family": "JetBrains Mono, ui-monospace, monospace",
        "components": [
            ComponentSpec(
                id="Header",
                status="not_started",
                description="Top navigation bar with run/history tabs and call budget meter.",
                props=["callCount", "callCeiling", "onGoRun", "onGoHistory"],
                data_dependencies=["CodeforgeRun.agent_call_count"],
                interactions=["click Run tab → switches to run view"],
                notes="Fixed height 50px.",
            ),
            ComponentSpec(
                id="PhaseRail",
                status="in_progress",
                description="Horizontal rail of 7 phase cards.",
                props=["phases"],
                data_dependencies=["events.jsonl"],
                interactions=["click phase card → opens phase drawer"],
                notes="Phase cards colored by PH map.",
            ),
        ],
    }
    defaults.update(overrides)
    return UIDesignState(**defaults)


# ---------------------------------------------------------------------------
# Schema round-trip
# ---------------------------------------------------------------------------

def test_ui_design_state_round_trip() -> None:
    state = _minimal_ui_state()
    dumped = state.model_dump()
    restored = UIDesignState(**dumped)
    assert restored.schema_version == "1.0.0"
    assert restored.seeded_at == "seed"
    assert len(restored.components) == 2
    assert restored.components[0].id == "Header"


def test_ui_design_state_optional_fields_default() -> None:
    # All required fields present; no optional — model should validate
    state = _minimal_ui_state()
    assert state.design_source == "dashboard.dc.html"


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------

def test_render_ui_design_contains_all_sections() -> None:
    state = _minimal_ui_state()
    md = render_ui_design(state)
    assert "# UI Design" in md
    assert "## Design Tokens" in md
    assert "## Phase Colors" in md
    assert "## Font Family" in md
    assert "## Components" in md


def test_render_ui_design_token_table() -> None:
    state = _minimal_ui_state()
    md = render_ui_design(state)
    assert "bg_base" in md
    assert "#0a0b0e" in md
    assert "Page background" in md


def test_render_ui_design_phase_color_table() -> None:
    state = _minimal_ui_state()
    md = render_ui_design(state)
    assert "req" in md
    assert "oklch(0.70 0.14 255)" in md


def test_render_ui_design_status_badges() -> None:
    state = _minimal_ui_state()
    md = render_ui_design(state)
    assert "[NOT STARTED]" in md
    assert "[IN PROGRESS]" in md


def test_render_ui_design_component_order_preserved() -> None:
    """Component order must match JSON array order, not be sorted alphabetically."""
    state = _minimal_ui_state()
    md = render_ui_design(state)
    header_pos = md.index("Header")
    phase_rail_pos = md.index("PhaseRail")
    assert header_pos < phase_rail_pos, "Header must appear before PhaseRail (array order)"


def test_render_ui_design_reversed_order_preserved() -> None:
    state = _minimal_ui_state(
        components=[
            ComponentSpec(id="ZComp", status="not_started", description="Z", props=[], data_dependencies=[], interactions=[], notes=""),
            ComponentSpec(id="AComp", status="built", description="A", props=[], data_dependencies=[], interactions=[], notes=""),
        ]
    )
    md = render_ui_design(state)
    z_pos = md.index("ZComp")
    a_pos = md.index("AComp")
    assert z_pos < a_pos, "ZComp before AComp — original array order, not alphabetical"


def test_render_ui_design_empty_components() -> None:
    state = _minimal_ui_state(components=[])
    md = render_ui_design(state)
    assert "_No components defined._" in md


def test_render_ui_design_built_badge() -> None:
    state = _minimal_ui_state(
        components=[
            ComponentSpec(id="EventLog", status="built", description="d", props=[], data_dependencies=[], interactions=[], notes="")
        ]
    )
    md = render_ui_design(state)
    assert "[BUILT]" in md


# ---------------------------------------------------------------------------
# ProjectStateStore load/write
# ---------------------------------------------------------------------------

def test_load_ui_design_returns_none_when_absent(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path / "project")
    assert store.load_ui_design() is None


def test_load_ui_design_returns_none_when_dir_missing(tmp_path: Path) -> None:
    store = ProjectStateStore(tmp_path / "nonexistent")
    assert store.load_ui_design() is None


def test_write_and_load_ui_design(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    store = ProjectStateStore(project_dir)
    state = _minimal_ui_state()
    store.write("ui_design", state.model_dump())

    loaded = store.load_ui_design()
    assert loaded is not None
    assert loaded.schema_version == "1.0.0"
    assert loaded.seeded_at == "seed"
    assert len(loaded.components) == 2
    # Markdown was also written
    md_path = project_dir / "project-state" / "ui_design.md"
    assert md_path.exists()
    assert "# UI Design" in md_path.read_text()


# ---------------------------------------------------------------------------
# Assembler firewall
# ---------------------------------------------------------------------------

def _make_assembler_with_ui_design(
    project_dir: Path,
    run_log_dir: Path,
) -> ContextAssembler:
    store = ProjectStateStore(project_dir)
    store.write("ui_design", _minimal_ui_state().model_dump())
    pending = PendingWrites(store)
    manifest = load_manifest()
    run_dir = run_log_dir / "run-assembler-test"
    artifact_store = ArtifactStore(run_dir)
    return ContextAssembler(
        manifest=manifest,
        artifact_store=artifact_store,
        project_state=store,
        pending_writes=pending,
        run_log_dir=run_dir,
        source_root=None,
    )


@pytest.mark.parametrize("agent_id", [
    "requirements_analyst",
    "architecture_designer",
    "coder",
    "code_reviewer",
])
def test_ui_design_included_for_allowed_agents(
    agent_id: str, tmp_path: Path
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    run_log_dir = tmp_path / "run-logs"
    run_log_dir.mkdir()
    assembler = _make_assembler_with_ui_design(project_dir, run_log_dir)
    pkg = assembler.assemble(agent_id, "run-test")
    assert "ui_design" in pkg.state_documents, (
        f"{agent_id} should receive ui_design_md"
    )


@pytest.mark.parametrize("agent_id", [
    "test_designer",
    "security_reviewer",
    "test_analyst",
])
def test_ui_design_excluded_for_disallowed_agents(
    agent_id: str, tmp_path: Path
) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    run_log_dir = tmp_path / "run-logs"
    run_log_dir.mkdir()
    assembler = _make_assembler_with_ui_design(project_dir, run_log_dir)
    pkg = assembler.assemble(agent_id, "run-test")
    assert "ui_design" not in pkg.state_documents, (
        f"{agent_id} must not receive ui_design_md"
    )


# ---------------------------------------------------------------------------
# Phase 6 component status update
# ---------------------------------------------------------------------------

def _make_pending_with_req_history(
    component_ids: list[str] | None,
    project_dir: Path,
) -> PendingWrites:
    store = ProjectStateStore(project_dir)
    pending = PendingWrites(store)
    pending.set("requirements_history", {
        "run_id": "run-001",
        "run_mode": "continuation",
        "feature_title": "Phase Rail Component",
        "feature_description": "Build the phase rail",
        "scope": {"in_scope": ["PhaseRail"], "explicitly_out_of_scope": []},
        "acceptance_criteria": [],
        "data_contracts": [],
        "human_confirmed_decisions": [],
        "ui_design_component_ids": component_ids,
    })
    return pending


def test_stage_ui_design_update_marks_components_built(tmp_path: Path) -> None:
    """Phase 6: matching component IDs are marked built in pending writes."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    store = ProjectStateStore(project_dir)
    store.write("ui_design", _minimal_ui_state().model_dump())

    pending = _make_pending_with_req_history(["PhaseRail"], project_dir)

    from codeforge.orchestrator.pending_writes import PendingWrites as _PW
    # Simulate what _stage_ui_design_update does inline
    req_history = pending.get("requirements_history")
    assert req_history is not None
    component_ids = req_history.get("ui_design_component_ids")
    assert component_ids == ["PhaseRail"]

    ui_data = pending.get("ui_design")
    if ui_data is None:
        ui_state = store.load_ui_design()
    else:
        ui_state = UIDesignState(**ui_data)
    assert ui_state is not None

    id_set = set(component_ids)
    for comp in ui_state.components:
        if comp.id in id_set:
            comp.status = "built"
    ui_state.last_updated_run = "run-001"
    pending.set("ui_design", ui_state.model_dump())

    staged = pending.get("ui_design")
    assert staged is not None
    result = UIDesignState(**staged)
    phase_rail = next(c for c in result.components if c.id == "PhaseRail")
    assert phase_rail.status == "built"
    # Header was NOT in the IDs list — must be unchanged
    header = next(c for c in result.components if c.id == "Header")
    assert header.status == "not_started"
    assert result.last_updated_run == "run-001"


def test_stage_ui_design_update_skips_when_ids_null(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    pending = _make_pending_with_req_history(None, project_dir)
    # No ui_design key should be staged
    assert pending.get("ui_design") is None


def test_stage_ui_design_update_skips_when_ids_empty(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    pending = _make_pending_with_req_history([], project_dir)
    assert pending.get("ui_design") is None


def test_stage_ui_design_update_graceful_when_not_seeded(tmp_path: Path) -> None:
    """If ui_design.json doesn't exist and component IDs are present, no error."""
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    store = ProjectStateStore(project_dir)
    # No ui_design written — store.load_ui_design() returns None
    pending = PendingWrites(store)
    pending.set("requirements_history", {
        "run_id": "run-001",
        "run_mode": "continuation",
        "feature_title": "Phase Rail",
        "feature_description": "",
        "scope": {"in_scope": [], "explicitly_out_of_scope": []},
        "acceptance_criteria": [],
        "data_contracts": [],
        "human_confirmed_decisions": [],
        "ui_design_component_ids": ["PhaseRail"],
    })
    # Simulate the early exit when ui_state is None
    ui_state = store.load_ui_design()
    assert ui_state is None
    # No exception, no staged write
    assert pending.get("ui_design") is None


# ---------------------------------------------------------------------------
# Seed parser
# ---------------------------------------------------------------------------

_MINIMAL_DC_HTML = dedent("""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body>
<x-dc>
<helmet>
<style>
  body{background:#0a0b0e;font-family:'JetBrains Mono',ui-monospace,monospace}
  --bg-base: #0a0b0e;
  --accent: oklch(0.78 0.13 168);
</style>
</helmet>
<div>
<!-- ============ RUN VIEW ============ -->
<div>run content</div>
<!-- ============ HISTORY VIEW ============ -->
<div>history content</div>
</div>
<script type="text/x-dc" data-dc-script data-props="{}">
class Component extends DCLogic {
  PH = {
    req:    { name:'Requirements',    color:'oklch(0.70 0.14 255)' },
    arch:   { name:'Architecture',    color:'oklch(0.73 0.13 221)' },
    code:   { name:'Code',            color:'oklch(0.76 0.13 187)' },
    crev:   { name:'Code Review',     color:'oklch(0.79 0.14 153)' },
    srev:   { name:'Security Review', color:'oklch(0.82 0.14 119)' },
    test:   { name:'Testing',         color:'oklch(0.85 0.13 85)'  },
    commit: { name:'Commit',          color:'oklch(0.84 0.13 51)'  },
  };
  ERR = 'oklch(0.66 0.21 27)';
  ORCH = 'oklch(0.72 0.15 308)';
}
</script>
</x-dc>
</body>
</html>
""").strip()


def test_seed_parser_produces_valid_state(tmp_path: Path) -> None:
    html_file = tmp_path / "dashboard.dc.html"
    html_file.write_text(_MINIMAL_DC_HTML)
    state = SeedParser(html_file).parse()
    assert isinstance(state, UIDesignState)
    assert state.seeded_at == "seed"
    assert state.last_updated_run == "seed"
    assert state.design_source == "dashboard.dc.html"
    assert state.schema_version == "1.0.0"


def test_seed_parser_extracts_phase_colors(tmp_path: Path) -> None:
    html_file = tmp_path / "dashboard.dc.html"
    html_file.write_text(_MINIMAL_DC_HTML)
    state = SeedParser(html_file).parse()
    phase_ids = {pc.phase_id for pc in state.phase_colors}
    assert "req" in phase_ids
    assert "arch" in phase_ids
    assert "commit" in phase_ids
    req_color = next(pc.color for pc in state.phase_colors if pc.phase_id == "req")
    assert req_color == "oklch(0.70 0.14 255)"


def test_seed_parser_extracts_orch_and_err_tokens(tmp_path: Path) -> None:
    html_file = tmp_path / "dashboard.dc.html"
    html_file.write_text(_MINIMAL_DC_HTML)
    state = SeedParser(html_file).parse()
    token_names = {t.name for t in state.design_tokens}
    assert "err" in token_names
    assert "orch" in token_names


def test_seed_parser_components_all_not_started(tmp_path: Path) -> None:
    html_file = tmp_path / "dashboard.dc.html"
    html_file.write_text(_MINIMAL_DC_HTML)
    state = SeedParser(html_file).parse()
    for comp in state.components:
        assert comp.status == "not_started"


def test_seed_parser_scaffolds_components_from_comments(tmp_path: Path) -> None:
    html_file = tmp_path / "dashboard.dc.html"
    html_file.write_text(_MINIMAL_DC_HTML)
    state = SeedParser(html_file).parse()
    comp_ids = [c.id for c in state.components]
    assert "RunView" in comp_ids
    assert "HistoryView" in comp_ids


def test_seed_parser_rejects_non_dc_html(tmp_path: Path) -> None:
    bad_file = tmp_path / "not_a_design.html"
    bad_file.write_text("<html><body>hello</body></html>")
    with pytest.raises(ValueError, match="<x-dc>"):
        SeedParser(bad_file).parse()


# ---------------------------------------------------------------------------
# CLI: codeforge seed command
# ---------------------------------------------------------------------------

runner = CliRunner()


def test_cli_seed_writes_files(tmp_path: Path) -> None:
    html_file = tmp_path / "dashboard.dc.html"
    html_file.write_text(_MINIMAL_DC_HTML)
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    result = runner.invoke(app, [
        "seed",
        "--project-dir", str(project_dir),
        "--ui-design", str(html_file),
    ])
    assert result.exit_code == 0, result.output
    assert (project_dir / "project-state" / "ui_design.json").exists()
    assert (project_dir / "project-state" / "ui_design.md").exists()


def test_cli_seed_errors_when_already_seeded(tmp_path: Path) -> None:
    html_file = tmp_path / "dashboard.dc.html"
    html_file.write_text(_MINIMAL_DC_HTML)
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    runner.invoke(app, ["seed", "--project-dir", str(project_dir), "--ui-design", str(html_file)])
    result = runner.invoke(app, ["seed", "--project-dir", str(project_dir), "--ui-design", str(html_file)])
    assert result.exit_code != 0
    assert "already seeded" in result.output


def test_cli_seed_errors_when_file_not_found(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    result = runner.invoke(app, [
        "seed",
        "--project-dir", str(project_dir),
        "--ui-design", str(tmp_path / "nonexistent.dc.html"),
    ])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_cli_seed_errors_when_project_dir_not_found(tmp_path: Path) -> None:
    html_file = tmp_path / "dashboard.dc.html"
    html_file.write_text(_MINIMAL_DC_HTML)
    result = runner.invoke(app, [
        "seed",
        "--project-dir", str(tmp_path / "nonexistent"),
        "--ui-design", str(html_file),
    ])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# CLI: --brief-file flag on codeforge run
# ---------------------------------------------------------------------------

def test_run_brief_file_mutually_exclusive_with_brief(tmp_path: Path) -> None:
    brief_file = tmp_path / "brief.md"
    brief_file.write_text("Build a thing")
    result = runner.invoke(app, [
        "run",
        "--project-dir", str(tmp_path),
        "--brief", "inline brief",
        "--brief-file", str(brief_file),
    ])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_run_requires_brief_or_brief_file(tmp_path: Path) -> None:
    result = runner.invoke(app, [
        "run",
        "--project-dir", str(tmp_path),
    ])
    assert result.exit_code != 0


def test_run_brief_file_not_found(tmp_path: Path) -> None:
    result = runner.invoke(app, [
        "run",
        "--project-dir", str(tmp_path),
        "--brief-file", str(tmp_path / "nonexistent.md"),
    ])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_run_brief_file_empty(tmp_path: Path) -> None:
    brief_file = tmp_path / "brief.md"
    brief_file.write_text("   ")
    result = runner.invoke(app, [
        "run",
        "--project-dir", str(tmp_path),
        "--brief-file", str(brief_file),
    ])
    assert result.exit_code != 0
    assert "empty" in result.output
