"""
Artifact store isolation tests.

Guards the invariant that failed (blocked / low-confidence) artifacts written via
write_failed() are recoverable for debugging but NEVER surface through the
get_latest / get_latest_meta / exists queries — otherwise the context assembler or
a resumed run could silently consume a blocked output as if it were valid.
"""
from __future__ import annotations

from pathlib import Path

from codeforge.schemas.contracts import AgentOutput
from codeforge.store.artifact_store import ArtifactStore


def _output(summary: str, confidence: float = 0.9) -> AgentOutput:
    return AgentOutput(
        output={"verdict": "error", "summary": summary},
        assumptions_made=[],
        confidence=confidence,
        unresolved_flags=[],
    )


def _write(store: ArtifactStore, *, failed: bool, summary: str):
    writer = store.write_failed if failed else store.write
    return writer(
        artifact_type="test_analysis",
        produced_by="test_analyst",
        output=_output(summary),
        run_id="run-test",
        codeforge_version="codeforge-v1",
        schema_version="1.0.0",
        allowed_consumers=[],
        forbidden_consumers=[],
    )


def test_failed_artifact_is_invisible_to_queries(run_log_dir: Path) -> None:
    store = ArtifactStore(run_log_dir)

    # No artifacts at all -> queries empty.
    assert store.exists("test_analysis") is False
    assert store.get_latest("test_analysis") is None

    # Write a failed artifact only.
    meta = _write(store, failed=True, summary="blocked output")

    # It exists on disk under failed_artifacts/ ...
    assert (run_log_dir / "failed_artifacts" / f"{meta.artifact_id}.json").exists()
    # ... but is invisible to every artifacts/ query.
    assert store.exists("test_analysis") is False
    assert store.get_latest("test_analysis") is None
    assert store.get_latest_meta("test_analysis") is None


def test_failed_write_does_not_override_latest_valid(run_log_dir: Path) -> None:
    store = ArtifactStore(run_log_dir)

    _write(store, failed=False, summary="the valid output")
    # A LATER failed write must not become "latest" — newest-wins must ignore it.
    _write(store, failed=True, summary="blocked output")

    latest = store.get_latest("test_analysis")
    assert latest is not None
    assert latest.output["summary"] == "the valid output"
    assert store.exists("test_analysis") is True
