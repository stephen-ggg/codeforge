"""
Assembler firewall tests.

Guards the invariant that code_artifact is never included in the context
package assembled for test_designer, even when the artifact exists in the store.
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


@pytest.fixture
def assembler_with_code_artifact(project_dir: Path, run_log_dir: Path) -> ContextAssembler:
    """
    Assembler wired to a store that contains a code_artifact.
    test_designer must not receive it.
    """
    run_dir = run_log_dir / "run-test"
    artifact_store = ArtifactStore(run_dir)
    artifact_store.write(
        artifact_type="code_artifact",
        produced_by="coder",
        output=AgentOutput(
            output={"files": [], "dependencies": [], "dev_dependencies": []},
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
