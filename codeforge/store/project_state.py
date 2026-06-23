"""
store/project_state.py — Project state document store.

Each document is a JSON + markdown pair:
  <name>.json — source of truth; Pydantic-validated on load; orchestrator reads/writes this
  <name>.md   — deterministic render from JSON; never parsed; what agents receive

requirements_history/ is JSON-only (one file per run_id).

Critical constraint: this module reads JSON and writes JSON + markdown.
It NEVER parses markdown. If you find yourself parsing the .md file, fix the JSON schema.

All writes are called only from Phase 6 (CommitWriter flush via the orchestrator).
Mid-run, all writes go to the orchestrator's in-memory pending_writes map instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codeforge.schemas.contracts import (
    ArchitectureState,
    AssumptionsLog,
    DecisionsLog,
    FeatureRegistry,
    InterfaceSpec,
    ProjectStateDocument,
    RequirementsSummary,
    TechStackState,
    TestCoverageMap,
    UIDesignState,
    TechDecision,
    Assumption,
)
from codeforge.store.renderers.architecture import render_architecture
from codeforge.store.renderers.tech_stack import render_tech_stack
from codeforge.store.renderers.feature_registry import render_feature_registry
from codeforge.store.renderers.decisions_log import render_decisions_log
from codeforge.store.renderers.assumptions_log import render_assumptions_log
from codeforge.store.renderers.test_coverage_map import render_test_coverage_map
from codeforge.store.renderers.ui_design import render_ui_design


# ---------------------------------------------------------------------------
# Document → file name mapping
# ---------------------------------------------------------------------------
_DOC_FILENAME: dict[str, str] = {
    "architecture": "architecture",
    "tech_stack": "tech_stack",
    "feature_registry": "feature_registry",
    "decisions_log": "decisions_log",
    "assumptions_log": "assumptions_log",
    "test_coverage_map": "test_coverage_map",
    "ui_design": "ui_design",
    # requirements_history is handled separately (one file per run_id)
}


class ProjectStateStore:
    """
    Reads and writes project state documents from/to the project-state/ directory.

    The store enforces the JSON + markdown pair model:
      - read() returns parsed JSON (dict)
      - read_as_markdown() returns the rendered .md content
      - write() writes both .json and .md atomically

    The orchestrator reads pending_writes first during a run; this store
    is only read at run start (continuation mode) and written at Phase 6 flush.
    """

    def __init__(self, project_dir: Path) -> None:
        self._state_dir = project_dir / "project-state"
        self._history_dir = self._state_dir / "requirements_history"

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def read(self, document: ProjectStateDocument) -> dict[str, Any] | None:
        """
        Read a project state document as a parsed JSON dict.
        Returns None if the document does not exist yet (new project).
        """
        if document == "requirements_history":
            raise ValueError(
                "Use read_requirements_history(run_id) for requirements_history documents."
            )
        filename = _DOC_FILENAME[document]
        json_path = self._state_dir / f"{filename}.json"
        if not json_path.exists():
            return None
        return dict(json.loads(json_path.read_text(encoding="utf-8")))

    def read_as_markdown(self, document: ProjectStateDocument) -> str | None:
        """
        Render a project state document to markdown from its JSON source.
        Returns None if the document does not exist yet.

        Always renders from JSON so that manual edits to the .json are
        immediately visible to agents — the .md file on disk is a human-
        readable convenience artifact, not the authoritative source.
        """
        if document == "requirements_history":
            raise ValueError(
                "requirements_history has no markdown render — use read_requirements_history()."
            )
        data = self.read(document)
        if data is None:
            return None
        return self._render(document, data)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write(self, document: ProjectStateDocument, data: dict[str, Any]) -> None:
        """
        Write a project state document to disk as a JSON + markdown pair.

        Validates the data against the appropriate Pydantic model before writing.
        Creates the project-state/ directory if it does not exist.

        Called only from Phase 6 (CommitWriter flush). Mid-run writes go to
        the orchestrator's pending_writes map instead.
        """
        if document == "requirements_history":
            raise ValueError(
                "Use write_requirements_history(run_id, data) for requirements history."
            )

        self._state_dir.mkdir(parents=True, exist_ok=True)
        filename = _DOC_FILENAME[document]

        # Validate and render
        md_content = self._render(document, data)

        # Write JSON
        json_path = self._state_dir / f"{filename}.json"
        json_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Write markdown
        md_path = self._state_dir / f"{filename}.md"
        md_path.write_text(md_content, encoding="utf-8")

    # ------------------------------------------------------------------
    # Requirements history (JSON-only, one file per run_id)
    # ------------------------------------------------------------------

    def read_requirements_history(self, run_id: str) -> dict[str, Any] | None:
        """Read a single requirements history entry by run_id. Returns None if absent."""
        path = self._history_dir / f"{run_id}.json"
        if not path.exists():
            return None
        return dict(json.loads(path.read_text(encoding="utf-8")))

    def load_ui_design(self) -> UIDesignState | None:
        """Return the UIDesignState from disk, or None if not yet seeded."""
        data = self.read("ui_design")
        if data is None:
            return None
        return UIDesignState(**data)

    def list_requirements_history_run_ids(self) -> list[str]:
        """Return all run_ids that have a requirements history entry, sorted ascending."""
        if not self._history_dir.exists():
            return []
        return sorted(p.stem for p in self._history_dir.glob("*.json"))

    def write_requirements_history(self, run_id: str, data: dict[str, Any]) -> None:
        """Write a requirements history entry for the given run_id."""
        self._history_dir.mkdir(parents=True, exist_ok=True)
        path = self._history_dir / f"{run_id}.json"
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # RequirementsSummary projection (deterministic, no LLM)
    # ------------------------------------------------------------------

    def project_requirements_summary(self) -> RequirementsSummary:
        """
        Produce a RequirementsSummary from the current on-disk project state.

        Source mapping (per spec Part 6):
          completed_runs  ← requirements_history/*.json + decisions_log.json + feature_registry.json
          active_interfaces ← feature_registry.json non-deprecated features, stability: "stable"
          locked_tech_decisions ← tech_stack.json decisions where locked: true
          open_assumptions ← assumptions_log.json status: "open" and impact: "high"
        """
        completed_runs = self._project_completed_runs()
        active_interfaces = self._project_active_interfaces()
        locked_tech_decisions = self._project_locked_tech_decisions()
        open_assumptions = self._project_open_assumptions()

        return RequirementsSummary(
            completed_runs=completed_runs,
            active_interfaces=active_interfaces,
            locked_tech_decisions=locked_tech_decisions,
            open_assumptions=open_assumptions,
        )

    def _project_completed_runs(self) -> list[dict[str, Any]]:
        """
        Produce completed_runs entries from requirements_history + decisions_log + feature_registry.
        """
        run_ids = self.list_requirements_history_run_ids()
        if not run_ids:
            return []

        # Load decisions_log once for run_id filtering
        decisions_data = self.read("decisions_log")
        all_decisions: list[dict[str, Any]] = []
        if decisions_data:
            all_decisions = decisions_data.get("entries", [])

        # Load feature_registry for status lookup
        registry_data = self.read("feature_registry")
        feature_status_by_title: dict[str, str] = {}
        if registry_data:
            for feature in registry_data.get("features", []):
                feature_status_by_title[feature.get("feature_title", "")] = feature.get(
                    "status", "implemented"
                )

        results: list[dict[str, Any]] = []
        for run_id in run_ids:
            history = self.read_requirements_history(run_id)
            if not history:
                continue

            feature_title = history.get("feature_title", "")
            run_mode = history.get("run_mode", "new_project")

            # Key decisions for this run
            key_decisions = [
                d["decision"]
                for d in all_decisions
                if d.get("run_id") == run_id and d.get("entry_type") == "agent_decision"
            ]

            # Status: succeeded if feature is tested, failed_escalated otherwise
            feature_run_status = feature_status_by_title.get(feature_title)
            status = "succeeded" if feature_run_status == "tested" else "failed_escalated"

            results.append(
                {
                    "run_id": run_id,
                    "feature_title": feature_title,
                    "status": status,
                    "key_decisions": key_decisions,
                }
            )

        return results

    def _project_active_interfaces(self) -> list[InterfaceSpec]:
        """Non-deprecated features' interfaces where stability == 'stable'."""
        registry_data = self.read("feature_registry")
        if not registry_data:
            return []

        active: list[InterfaceSpec] = []
        for feature in registry_data.get("features", []):
            if feature.get("status") == "deprecated":
                continue
            for iface in feature.get("interfaces", []):
                if iface.get("stability") == "stable":
                    active.append(InterfaceSpec.model_validate(iface))
        return active

    def _project_locked_tech_decisions(self) -> list[TechDecision]:
        """tech_stack.json decisions where locked: true; supersedes-chain resolved to head."""
        tech_data = self.read("tech_stack")
        if not tech_data:
            return []

        all_decisions: list[dict[str, Any]] = tech_data.get("decisions", [])
        locked = [d for d in all_decisions if d.get("locked")]

        # Resolve supersedes chain: remove any decision that has been superseded
        superseded_ids: set[str] = set()
        for d in all_decisions:
            if d.get("supersedes"):
                superseded_ids.add(d["supersedes"])

        head_locked = [
            d for d in locked if d.get("id") not in superseded_ids
        ]

        return [TechDecision.model_validate(d) for d in head_locked]

    def _project_open_assumptions(self) -> list[Assumption]:
        """assumptions_log.json entries with status: 'open' and impact: 'high'."""
        assumptions_data = self.read("assumptions_log")
        if not assumptions_data:
            return []

        open_high: list[Assumption] = []
        for entry in assumptions_data.get("entries", []):
            if entry.get("status") == "open" and entry.get("impact") == "high":
                open_high.append(
                    Assumption(
                        id=entry["id"],
                        description=entry["description"],
                        impact=entry["impact"],
                        record=entry.get("record", True),
                    )
                )
        return open_high

    # ------------------------------------------------------------------
    # Internal: renderer dispatch
    # ------------------------------------------------------------------

    def _render(self, document: ProjectStateDocument, data: dict[str, Any]) -> str:
        """Dispatch to the correct renderer for a document type."""
        if document == "architecture":
            return render_architecture(ArchitectureState(**data))
        if document == "tech_stack":
            return render_tech_stack(TechStackState(**data))
        if document == "feature_registry":
            return render_feature_registry(FeatureRegistry(**data))
        if document == "decisions_log":
            return render_decisions_log(DecisionsLog(**data))
        if document == "assumptions_log":
            return render_assumptions_log(AssumptionsLog(**data))
        if document == "test_coverage_map":
            return render_test_coverage_map(TestCoverageMap(**data))
        if document == "ui_design":
            return render_ui_design(UIDesignState(**data))
        raise ValueError(f"No renderer for document type: {document!r}")