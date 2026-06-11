"""
firewall/manifest.py — Firewall manifest loader.

Loads manifest.yaml and returns a typed FirewallManifest covering:
  - Artifact access: which agents may read each artifact type
  - Project state access: which agents receive each state document in their context

The assembler reads FirewallManifest; it never makes judgment calls.
Every access decision is derived deterministically from this data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import yaml

from codeforge.schemas.contracts import ArtifactType, LogActor, ProjectStateDocument

# Default manifest path — relative to this file
_DEFAULT_MANIFEST_PATH = Path(__file__).parent / "manifest.yaml"


@dataclass
class ArtifactAccess:
    """Access rules for a single artifact type."""
    artifact_type: ArtifactType
    allowed_consumers: list[LogActor]
    forbidden_consumers: list[LogActor]

    def is_allowed(self, agent: LogActor) -> bool:
        """
        Return True if agent may access this artifact.
        Forbidden list is checked first and takes priority.
        """
        if agent in self.forbidden_consumers:
            return False
        # If allowed_consumers is non-empty, agent must be in it
        if self.allowed_consumers and agent not in self.allowed_consumers:
            return False
        return True


@dataclass
class ProjectStateAccess:
    """Access rules for a single project state document."""
    document: ProjectStateDocument
    allowed_readers: list[LogActor]

    def is_allowed(self, agent: LogActor) -> bool:
        """Return True if agent may receive this document in its context package."""
        return agent in self.allowed_readers


@dataclass
class FirewallManifest:
    """
    Fully loaded and typed firewall manifest.

    artifact_access: keyed by ArtifactType string
    project_state_access: keyed by ProjectStateDocument string
    """
    artifact_access: dict[str, ArtifactAccess] = field(default_factory=dict)
    project_state_access: dict[str, ProjectStateAccess] = field(default_factory=dict)

    def get_artifact_access(self, artifact_type: ArtifactType) -> ArtifactAccess | None:
        return self.artifact_access.get(artifact_type)

    def get_state_access(self, document: ProjectStateDocument) -> ProjectStateAccess | None:
        return self.project_state_access.get(document)


def load_manifest(manifest_path: Path | None = None) -> FirewallManifest:
    """
    Load and parse the firewall manifest YAML.

    Args:
        manifest_path: Path to manifest.yaml. Defaults to the bundled manifest.

    Returns:
        FirewallManifest with fully typed access tables.

    Raises:
        FileNotFoundError: manifest file not found.
        ValueError: manifest is missing required sections or contains unknown artifact types.
    """
    path = manifest_path or _DEFAULT_MANIFEST_PATH
    if not path.exists():
        raise FileNotFoundError(f"Firewall manifest not found at {path}")

    with path.open("r", encoding="utf-8") as fh:
        raw: dict[str, object] = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise ValueError(f"Manifest must be a YAML mapping, got {type(raw)}")

    manifest = FirewallManifest()

    # Parse artifact access
    raw_artifacts = raw.get("artifacts", {})
    if not isinstance(raw_artifacts, dict):
        raise ValueError("manifest.artifacts must be a mapping")

    for art_type_str, rules in raw_artifacts.items():
        if not isinstance(rules, dict):
            raise ValueError(f"manifest.artifacts.{art_type_str} must be a mapping")

        allowed = _parse_actor_list(rules.get("allowed_consumers", []), f"artifacts.{art_type_str}.allowed_consumers")
        forbidden = _parse_actor_list(rules.get("forbidden_consumers", []), f"artifacts.{art_type_str}.forbidden_consumers")

        manifest.artifact_access[art_type_str] = ArtifactAccess(
            artifact_type=cast(ArtifactType, art_type_str),
            allowed_consumers=allowed,
            forbidden_consumers=forbidden,
        )

    # Parse project state access
    raw_state = raw.get("project_state_access", {})
    if not isinstance(raw_state, dict):
        raise ValueError("manifest.project_state_access must be a mapping")

    for doc_str, rules in raw_state.items():
        if not isinstance(rules, dict):
            raise ValueError(f"manifest.project_state_access.{doc_str} must be a mapping")

        allowed = _parse_actor_list(rules.get("allowed_readers", []), f"project_state_access.{doc_str}.allowed_readers")

        manifest.project_state_access[doc_str] = ProjectStateAccess(
            document=cast(ProjectStateDocument, doc_str),
            allowed_readers=allowed,
        )

    return manifest


def _parse_actor_list(raw: object, context: str) -> list[LogActor]:
    """Parse a list of actor strings from the manifest, validating each."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"{context} must be a list, got {type(raw)}")

    valid_actors: set[str] = {
        "requirements_analyst", "architecture_designer", "coder",
        "code_reviewer", "security_reviewer", "test_designer",
        "test_analyst", "orchestrator", "commit_writer", "test_runner",
    }

    result: list[LogActor] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"{context}: expected string, got {type(item)}: {item!r}")
        if item not in valid_actors:
            raise ValueError(f"{context}: unknown actor {item!r}. Valid actors: {sorted(valid_actors)}")
        result.append(cast(LogActor, item))

    return result
