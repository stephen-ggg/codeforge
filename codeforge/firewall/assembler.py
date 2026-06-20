"""
firewall/assembler.py — Deterministic context package builder.

Assembles per-agent context packages by reading the firewall manifest.
No judgment calls — every inclusion and exclusion is rule-derived.
Every access decision (allow OR deny) is logged as an AccessEvent.

The full context package is written to:
  run-logs/<run_id>/context_packages/<assembly_id>.json

This file is the firewall audit surface — it records exactly what each agent saw.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from codeforge.schemas.contracts import (
    AccessEvent,
    AgentId,
    AgentOutput,
    ArtifactType,
    CodeArtifact,
    LogActor,
    ProjectStateDocument,
)
from codeforge.store.artifact_store import ArtifactStore
from codeforge.store.edits import resolve_code_artifact_edits
from codeforge.store.project_state import ProjectStateStore
from codeforge.firewall.manifest import FirewallManifest


# ---------------------------------------------------------------------------
# PendingWrites protocol — assembler depends on this interface, not the
# concrete orchestrator implementation (avoids a circular import in Stage 6).
# ---------------------------------------------------------------------------

@runtime_checkable
class PendingWritesProtocol(Protocol):
    """Minimal interface the assembler needs from PendingWrites."""

    def get(self, document: ProjectStateDocument) -> dict[str, Any] | None:
        """Return the pending in-memory write for document, or None if not pending."""
        ...


# ---------------------------------------------------------------------------
# Context package — what gets delivered to each agent
# ---------------------------------------------------------------------------

class ContextPackage:
    """
    The assembled context for one agent invocation.

    artifacts: artifact_type → AgentOutput (only types the agent is allowed to see)
    state_documents: document_name → markdown string (only docs the agent is allowed to read)
    access_events: full audit trail for this assembly
    assembly_id: groups all events for this one context build
    """

    def __init__(
        self,
        agent_id: AgentId,
        run_id: str,
        assembly_id: str,
    ) -> None:
        self.agent_id = agent_id
        self.run_id = run_id
        self.assembly_id = assembly_id
        self.artifacts: dict[str, AgentOutput[Any]] = {}
        self.state_documents: dict[str, str] = {}
        self.access_events: list[AccessEvent] = []

    def to_dict(self) -> dict[str, Any]:
        """Serialise for writing to context_packages/<assembly_id>.json."""
        return {
            "assembly_id": self.assembly_id,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "artifacts": {
                k: v.model_dump() for k, v in self.artifacts.items()
            },
            "state_documents": self.state_documents,
            "access_events": [e.model_dump() for e in self.access_events],
        }


# ---------------------------------------------------------------------------
# Assembler
# ---------------------------------------------------------------------------

# All artifact types codeforge currently produces.
# The assembler iterates these to check what's available in the artifact store.
_ALL_ARTIFACT_TYPES: list[ArtifactType] = [
    "requirements_doc",
    "architecture_doc",
    "interface_manifest",
    "code_artifact",
    "module_interfaces",
    "review_report",
    "security_report",
    "test_suite",
    "test_results",
    "test_analysis",
]

# All project state documents.
_ALL_STATE_DOCUMENTS: list[ProjectStateDocument] = [
    "architecture",
    "tech_stack",
    "feature_registry",
    "decisions_log",
    "assumptions_log",
    "test_coverage_map",
]


class ContextAssembler:
    """
    Deterministic context package builder.

    Reads the firewall manifest to decide what each agent is allowed to see.
    Never makes a judgment call — all decisions are manifest-derived.
    Logs every access decision as an AccessEvent.
    """

    def __init__(
        self,
        manifest: FirewallManifest,
        artifact_store: ArtifactStore,
        project_state: ProjectStateStore,
        pending_writes: PendingWritesProtocol,
        run_log_dir: Path,
        source_root: Path | None = None,
    ) -> None:
        self._manifest = manifest
        self._artifact_store = artifact_store
        self._project_state = project_state
        self._pending_writes = pending_writes
        self._source_root = source_root
        self._context_packages_dir = run_log_dir / "context_packages"
        self._context_packages_dir.mkdir(parents=True, exist_ok=True)

    def assemble(self, agent_id: AgentId, run_id: str) -> ContextPackage:
        """
        Build a context package for agent_id.

        Steps:
          1. Create a new ContextPackage with a fresh assembly_id
          2. For each artifact type: check manifest, log decision, include if allowed
          3. For each state document: check manifest, read pending_writes first
             then fall back to disk, include if allowed
          4. Write full package to context_packages/<assembly_id>.json
          5. Return the package

        Every allow AND deny decision is recorded as an AccessEvent.
        """
        assembly_id = str(uuid.uuid4())
        package = ContextPackage(agent_id=agent_id, run_id=run_id, assembly_id=assembly_id)

        # Step 2 — Artifacts
        for artifact_type in _ALL_ARTIFACT_TYPES:
            self._assemble_artifact(package, agent_id, artifact_type, assembly_id)

        # Step 3 — Project state documents
        for document in _ALL_STATE_DOCUMENTS:
            self._assemble_state_document(package, agent_id, document, assembly_id)

        # Step 4 — Write audit package to disk
        self._write_context_package(package)

        return package

    def _assemble_artifact(
        self,
        package: ContextPackage,
        agent_id: AgentId,
        artifact_type: ArtifactType,
        assembly_id: str,
    ) -> None:
        """Check manifest and include artifact if allowed. Log the decision either way."""
        # Only include artifacts that actually exist in the store this run
        if not self._artifact_store.exists(artifact_type):
            return

        access_rules = self._manifest.get_artifact_access(artifact_type)
        actor: LogActor = agent_id  # AgentId is a subset of LogActor

        if access_rules is None or not access_rules.is_allowed(actor):
            # Deny — log and skip
            reason = (
                "agent in forbidden_consumers"
                if access_rules and actor in access_rules.forbidden_consumers
                else "agent not in allowed_consumers"
            )
            package.access_events.append(
                AccessEvent(
                    artifact_id=f"type:{artifact_type}",
                    requesting_agent=actor,
                    decision="deny",
                    reason_code=reason,
                    assembly_id=assembly_id,
                    timestamp=_now(),
                )
            )
            return

        # Allow — retrieve and include
        output = self._artifact_store.get_latest(artifact_type)
        if output is None:
            return  # exists() returned True but get_latest returned None — race; skip

        # For code_artifact, resolve any edits-only files to full content so
        # review agents (code_reviewer, security_reviewer) see complete file bodies.
        if artifact_type == "code_artifact" and self._source_root is not None:
            code = CodeArtifact.model_validate(output.output)
            code = resolve_code_artifact_edits(code, self._source_root)
            output = output.model_copy(update={"output": code.model_dump()})

        meta = self._artifact_store.get_latest_meta(artifact_type)
        artifact_id = meta.artifact_id if meta else f"type:{artifact_type}"

        package.access_events.append(
            AccessEvent(
                artifact_id=artifact_id,
                requesting_agent=actor,
                decision="allow",
                reason_code="in_allowed_consumers",
                assembly_id=assembly_id,
                timestamp=_now(),
            )
        )
        package.artifacts[artifact_type] = output

    def _assemble_state_document(
        self,
        package: ContextPackage,
        agent_id: AgentId,
        document: ProjectStateDocument,
        assembly_id: str,
    ) -> None:
        """Check manifest and include state document markdown if allowed. Log either way."""
        access_rules = self._manifest.get_state_access(document)
        actor: LogActor = agent_id

        if access_rules is None or not access_rules.is_allowed(actor):
            package.access_events.append(
                AccessEvent(
                    artifact_id=f"state:{document}",
                    requesting_agent=actor,
                    decision="deny",
                    reason_code="agent not in allowed_readers",
                    assembly_id=assembly_id,
                    timestamp=_now(),
                )
            )
            return

        # Read from pending_writes first, fall back to disk
        md: str | None
        pending = self._pending_writes.get(document)
        if pending is not None:
            # Render the pending JSON to markdown for the agent
            md = self._project_state._render(document, pending)
        else:
            md = self._project_state.read_as_markdown(document)

        if md is None:
            # Document doesn't exist yet (new project) — don't include, don't log deny
            return

        package.access_events.append(
            AccessEvent(
                artifact_id=f"state:{document}",
                requesting_agent=actor,
                decision="allow",
                reason_code="in_allowed_readers",
                assembly_id=assembly_id,
                timestamp=_now(),
            )
        )
        package.state_documents[document] = md

    def persist(self, package: ContextPackage) -> None:
        """Re-write a context package to disk after later mutation.

        Tool-enabled agents accumulate AccessEvents during their tool loop; the
        orchestrator appends them to the package and calls this so the on-disk
        audit surface records every read the agent made, not just assembly-time
        decisions.
        """
        self._write_context_package(package)

    def _write_context_package(self, package: ContextPackage) -> None:
        """Write the full context package to disk for audit purposes."""
        path = self._context_packages_dir / f"{package.assembly_id}.json"
        path.write_text(
            json.dumps(package.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
