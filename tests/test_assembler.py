"""
Assembler firewall tests.

Guards the invariant that code_artifact is never included in the context
package assembled for test_designer, even when the artifact exists in the store.

Also verifies that edits-only files in a code_artifact are resolved to full
content before delivery to review agents (code_reviewer, security_reviewer).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from codeforge.firewall.assembler import ContextAssembler
from codeforge.firewall.manifest import load_manifest
from codeforge.orchestrator.pending_writes import PendingWrites
from codeforge.schemas.contracts import AgentOutput
from codeforge.store.artifact_store import ArtifactStore
from codeforge.store.project_state import ProjectStateStore


def _make_assembler(
    project_dir: Path,
    run_log_dir: Path,
    artifact_output: dict,
    *,
    source_root: Path | None = None,
) -> ContextAssembler:
    run_dir = run_log_dir / "run-test"
    artifact_store = ArtifactStore(run_dir)
    artifact_store.write(
        artifact_type="code_artifact",
        produced_by="coder",
        output=AgentOutput(
            output=artifact_output,
            assumptions_made=[],
            confidence=0.9,
            unresolved_flags=[],
        ),
        run_id="run-test",
        codeforge_version="codeforge-v1",
        schema_version="1.0.0",
        allowed_consumers=["code_reviewer", "security_reviewer", "test_runner"],
        forbidden_consumers=["test_designer", "test_analyst"],
    )
    project_state = ProjectStateStore(project_dir)
    pending = PendingWrites(project_state)
    manifest = load_manifest()
    return ContextAssembler(
        manifest=manifest,
        artifact_store=artifact_store,
        project_state=project_state,
        pending_writes=pending,
        run_log_dir=run_dir,
        source_root=source_root,
    )


@pytest.fixture
def assembler_with_code_artifact(project_dir: Path, run_log_dir: Path) -> ContextAssembler:
    """Assembler wired to a store that contains a code_artifact. test_designer must not receive it."""
    return _make_assembler(
        project_dir,
        run_log_dir,
        {"files": [], "change_summary": "", "criteria_addressed": [], "interface_changes": []},
    )


def test_code_artifact_excluded_from_test_designer(
    assembler_with_code_artifact: ContextAssembler,
) -> None:
    pkg = assembler_with_code_artifact.assemble("test_designer", "run-test")
    assert "code_artifact" not in pkg.artifacts


def test_deny_event_logged_for_code_artifact(
    assembler_with_code_artifact: ContextAssembler,
) -> None:
    pkg = assembler_with_code_artifact.assemble("test_designer", "run-test")
    deny_events = [
        e for e in pkg.access_events
        if e.artifact_id == "type:code_artifact" and e.decision == "deny"
    ]
    assert len(deny_events) == 1
    assert deny_events[0].requesting_agent == "test_designer"


def test_assembler_resolves_edits_for_reviewer(
    project_dir: Path, run_log_dir: Path, tmp_path: Path
) -> None:
    """code_reviewer and security_reviewer receive full file content, not empty-string + edits."""
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "runs.ts").write_text("export const x = 1;\n")

    artifact_output = {
        "files": [
            {
                "path": "lib/runs.ts",
                "content": "",
                "language": "typescript",
                "change_type": "modified",
                "change_reason": None,
                "edits": [{"old_string": "x = 1", "new_string": "x = 99"}],
            }
        ],
        "change_summary": "bump x",
        "criteria_addressed": [],
        "interface_changes": [],
    }

    assembler = _make_assembler(project_dir, run_log_dir, artifact_output, source_root=tmp_path)

    for agent in ("code_reviewer", "security_reviewer"):
        pkg = assembler.assemble(agent, "run-test")
        code_art = pkg.artifacts["code_artifact"]
        files = code_art.output["files"]
        assert files[0]["content"] == "export const x = 99;\n", (
            f"{agent} received empty content instead of resolved file body"
        )


def test_assembler_no_source_root_passes_through_raw(
    project_dir: Path, run_log_dir: Path
) -> None:
    """Without source_root, edits-only files are passed through unchanged (no crash)."""
    artifact_output = {
        "files": [
            {
                "path": "lib/runs.ts",
                "content": "",
                "language": "typescript",
                "change_type": "modified",
                "change_reason": None,
                "edits": [{"old_string": "x = 1", "new_string": "x = 99"}],
            }
        ],
        "change_summary": "bump x",
        "criteria_addressed": [],
        "interface_changes": [],
    }
    assembler = _make_assembler(project_dir, run_log_dir, artifact_output, source_root=None)
    pkg = assembler.assemble("code_reviewer", "run-test")
    files = pkg.artifacts["code_artifact"].output["files"]
    assert files[0]["content"] == ""
