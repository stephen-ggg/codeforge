"""
store/artifact_store.py — Filesystem-backed artifact store.

Each codeforge run gets its own directory under run-logs/<run_id>/:
  artifacts/         — validated agent outputs (AgentOutput + ArtifactMeta), one JSON per artifact
  raw_outputs/       — raw LLM responses that failed validation and were dropped, for debugging

Key behaviours:
  - write() stamps ArtifactMeta (UUID, ISO timestamp, SHA-256 content hash) and persists to disk
  - read() enforces allowed_consumers / forbidden_consumers before returning; raises AccessDeniedError
  - write_raw() stores a dropped, validation-failed output to raw_outputs/ — never mixed with validated artifacts
  - get_latest() returns the most recently written artifact of a given type, or None
  - exists() checks whether any artifact of a given type has been written this run
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codeforge.schemas.contracts import (
    AgentId,
    AgentOutput,
    ArtifactMeta,
    ArtifactType,
    LogActor,
)


class AccessDeniedError(Exception):
    """Raised when an agent attempts to read an artifact it is not permitted to access."""

    def __init__(self, artifact_id: str, requesting_agent: AgentId, reason: str) -> None:
        self.artifact_id = artifact_id
        self.requesting_agent = requesting_agent
        self.reason = reason
        super().__init__(
            f"Agent '{requesting_agent}' denied access to artifact '{artifact_id}': {reason}"
        )


class ArtifactNotFoundError(Exception):
    """Raised when an artifact_id does not exist in the store."""

    def __init__(self, artifact_id: str) -> None:
        self.artifact_id = artifact_id
        super().__init__(f"Artifact '{artifact_id}' not found in store")


# ---------------------------------------------------------------------------
# On-disk format
# ---------------------------------------------------------------------------
# Each artifact file is a JSON object with two top-level keys:
#   "meta"   — ArtifactMeta (orchestrator-stamped)
#   "output" — the full AgentOutput[T] payload
#
# raw_outputs files are plain strings (the raw LLM response) stored as:
#   {"artifact_id": "...", "raw": "..."}
# ---------------------------------------------------------------------------


class ArtifactStore:
    """
    Filesystem-backed store for validated codeforge artifacts.

    Args:
        run_dir: The per-run log directory, e.g. Path("my-project/run-logs/<run_id>").
                 The store creates artifacts/ and raw_outputs/ subdirectories within it.
    """

    def __init__(self, run_dir: Path) -> None:
        self._run_dir = run_dir
        self._artifacts_dir = run_dir / "artifacts"
        self._raw_outputs_dir = run_dir / "raw_outputs"
        # Blocked/low-confidence outputs go here, kept OUT of artifacts/ so the
        # get_latest/get_latest_meta/exists queries (which glob artifacts/) never
        # surface a failed output to the assembler or to resume. For debugging only.
        self._failed_dir = run_dir / "failed_artifacts"
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._raw_outputs_dir.mkdir(parents=True, exist_ok=True)
        self._failed_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Write validated artifact
    # ------------------------------------------------------------------

    def write(
        self,
        artifact_type: ArtifactType,
        produced_by: AgentId,
        output: AgentOutput[Any],
        run_id: str,
        codeforge_version: str,
        schema_version: str,
        allowed_consumers: list[LogActor],
        forbidden_consumers: list[LogActor],
    ) -> ArtifactMeta:
        """
        Persist a validated agent output to the artifact store.

        Stamps ArtifactMeta with a fresh UUID, ISO 8601 timestamp, and SHA-256 of
        the serialised output field. Returns the stamped ArtifactMeta.
        """
        return self._write_to(
            dest_dir=self._artifacts_dir,
            artifact_type=artifact_type,
            produced_by=produced_by,
            output=output,
            run_id=run_id,
            codeforge_version=codeforge_version,
            schema_version=schema_version,
            allowed_consumers=allowed_consumers,
            forbidden_consumers=forbidden_consumers,
        )

    def write_failed(
        self,
        artifact_type: ArtifactType,
        produced_by: AgentId,
        output: AgentOutput[Any],
        run_id: str,
        codeforge_version: str,
        schema_version: str,
        allowed_consumers: list[LogActor],
        forbidden_consumers: list[LogActor],
    ) -> ArtifactMeta:
        """
        Persist the output of a blocked / low-confidence (escalated) invocation.

        Identical on-disk shape to write(), but stored under failed_artifacts/ so it
        is recoverable for debugging (linked from the gate event and escalation record)
        WITHOUT being visible to get_latest/get_latest_meta/exists — which only glob
        artifacts/. This keeps a failed output from being silently consumed by the
        context assembler or by resume as if it were a valid artifact.
        """
        return self._write_to(
            dest_dir=self._failed_dir,
            artifact_type=artifact_type,
            produced_by=produced_by,
            output=output,
            run_id=run_id,
            codeforge_version=codeforge_version,
            schema_version=schema_version,
            allowed_consumers=allowed_consumers,
            forbidden_consumers=forbidden_consumers,
        )

    def _write_to(
        self,
        dest_dir: Path,
        artifact_type: ArtifactType,
        produced_by: AgentId,
        output: AgentOutput[Any],
        run_id: str,
        codeforge_version: str,
        schema_version: str,
        allowed_consumers: list[LogActor],
        forbidden_consumers: list[LogActor],
    ) -> ArtifactMeta:
        """Stamp ArtifactMeta, serialise, and write <artifact_id>.json into dest_dir."""
        artifact_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()

        # Serialise the output field for hashing and storage
        output_dict = output.model_dump()
        output_json = json.dumps(output_dict, sort_keys=True, ensure_ascii=False)
        content_hash = hashlib.sha256(output_json.encode()).hexdigest()

        meta = ArtifactMeta(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            produced_by=produced_by,
            run_id=run_id,
            codeforge_version=codeforge_version,
            schema_version=schema_version,
            created_at=created_at,
            content_hash=content_hash,
            allowed_consumers=allowed_consumers,
            forbidden_consumers=forbidden_consumers,
        )

        record: dict[str, Any] = {
            "meta": meta.model_dump(),
            "output": output_dict,
        }

        artifact_path = dest_dir / f"{artifact_id}.json"
        artifact_path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        return meta

    # ------------------------------------------------------------------
    # Write raw (pre-validation) output
    # ------------------------------------------------------------------

    def write_raw(self, raw: str, produced_by: str | None = None) -> str:
        """
        Persist a raw, pre-validation LLM response string and return its id.

        Goes to raw_outputs/<artifact_id>.json — isolated from validated artifacts and
        invisible to get_latest/get_latest_meta/exists. Used to capture an output that
        failed structural or contract validation and was about to be dropped, so the
        escalation record can link to it for debugging.
        """
        artifact_id = str(uuid.uuid4())
        raw_path = self._raw_outputs_dir / f"{artifact_id}.json"
        raw_path.write_text(
            json.dumps(
                {"artifact_id": artifact_id, "produced_by": produced_by, "raw": raw},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return artifact_id

    # ------------------------------------------------------------------
    # Read validated artifact
    # ------------------------------------------------------------------

    def read(
        self,
        artifact_id: str,
        requesting_agent: AgentId,
    ) -> AgentOutput[Any]:
        """
        Load a validated artifact and enforce access control.

        Raises:
            ArtifactNotFoundError: artifact_id does not exist.
            AccessDeniedError: requesting_agent is in forbidden_consumers,
                               or not in allowed_consumers (when list is non-empty).
        """
        artifact_path = self._artifacts_dir / f"{artifact_id}.json"
        if not artifact_path.exists():
            raise ArtifactNotFoundError(artifact_id)

        record: dict[str, Any] = json.loads(artifact_path.read_text(encoding="utf-8"))
        meta_data: dict[str, Any] = record["meta"]

        # Access control — forbidden list takes priority
        forbidden: list[str] = meta_data.get("forbidden_consumers", [])
        allowed: list[str] = meta_data.get("allowed_consumers", [])

        if requesting_agent in forbidden:
            raise AccessDeniedError(
                artifact_id,
                requesting_agent,
                f"agent is in forbidden_consumers for artifact type '{meta_data.get('artifact_type')}'",
            )

        if allowed and requesting_agent not in allowed:
            raise AccessDeniedError(
                artifact_id,
                requesting_agent,
                f"agent is not in allowed_consumers for artifact type '{meta_data.get('artifact_type')}'",
            )

        return AgentOutput[Any].model_validate(record["output"])

    # ------------------------------------------------------------------
    # Convenience queries
    # ------------------------------------------------------------------

    def get_latest(self, artifact_type: ArtifactType) -> AgentOutput[Any] | None:
        """
        Return the most recently written artifact of the given type, or None.

        Does NOT enforce access control — this is an internal orchestrator query.
        Access control is enforced at read() time when agents request artifacts.
        """
        candidates: list[tuple[str, dict[str, Any]]] = []

        for path in self._artifacts_dir.glob("*.json"):
            try:
                record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
                meta = record.get("meta", {})
                if meta.get("artifact_type") == artifact_type:
                    candidates.append((meta.get("created_at", ""), record))
            except (json.JSONDecodeError, KeyError):
                continue  # skip corrupt files

        if not candidates:
            return None

        # Sort by created_at ISO string — lexicographic sort works for ISO 8601
        candidates.sort(key=lambda x: x[0], reverse=True)
        return AgentOutput[Any].model_validate(candidates[0][1]["output"])

    def get_latest_meta(self, artifact_type: ArtifactType) -> ArtifactMeta | None:
        """
        Return the ArtifactMeta for the most recently written artifact of the given type.
        Used by the orchestrator to get artifact_ids for handoff events.
        """
        candidates: list[tuple[str, dict[str, Any]]] = []

        for path in self._artifacts_dir.glob("*.json"):
            try:
                record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
                meta = record.get("meta", {})
                if meta.get("artifact_type") == artifact_type:
                    candidates.append((meta.get("created_at", ""), record))
            except (json.JSONDecodeError, KeyError):
                continue

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[0], reverse=True)
        return ArtifactMeta.model_validate(candidates[0][1]["meta"])

    def exists(self, artifact_type: ArtifactType) -> bool:
        """Return True if any artifact of the given type exists in this run."""
        for path in self._artifacts_dir.glob("*.json"):
            try:
                record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
                if record.get("meta", {}).get("artifact_type") == artifact_type:
                    return True
            except (json.JSONDecodeError, KeyError):
                continue
        return False

    def get_meta(self, artifact_id: str) -> ArtifactMeta:
        """
        Return the ArtifactMeta for a specific artifact_id without loading the full output.
        Used for access event logging without pulling the full payload.
        """
        artifact_path = self._artifacts_dir / f"{artifact_id}.json"
        if not artifact_path.exists():
            raise ArtifactNotFoundError(artifact_id)

        record: dict[str, Any] = json.loads(artifact_path.read_text(encoding="utf-8"))
        return ArtifactMeta.model_validate(record["meta"])
