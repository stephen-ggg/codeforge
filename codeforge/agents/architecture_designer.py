"""
agents/architecture_designer.py — Architecture designer agent.

Produces module breakdown, data flow, interface definitions, tech decisions,
and an explicit AC-to-module criteria_coverage map.
Works in diff mode in continuation runs.
"""

from __future__ import annotations

import json
from typing import Any

from codeforge.agents.base import BaseAgent
from codeforge.firewall.assembler import ContextPackage
from codeforge.schemas.contracts import RePromptContext


class ArchitectureDesignerAgent(BaseAgent):
    """
    Formats the user turn for the architecture designer.

    Input context:
      - requirements_doc (from artifacts)
      - current_architecture_md (from state_documents)
      - tech_stack_md (from state_documents)
      - feature_registry_md (from state_documents)
      - spec_gap_context (injected by orchestrator on fail_spec_gap re-entry)
    """

    def build_user_turn(
        self,
        context_package: ContextPackage,
        reprompt: RePromptContext | None = None,
    ) -> str:
        state = context_package.state_documents
        artifacts = context_package.artifacts

        requirements_doc = None
        if "requirements_doc" in artifacts:
            requirements_doc = artifacts["requirements_doc"].model_dump()

        payload: dict[str, Any] = {
            "run_mode": state.get("_run_mode", "new_project"),
            "requirements_doc": requirements_doc,
            "current_architecture_md": state.get("architecture"),
            "tech_stack_md": state.get("tech_stack"),
            "feature_registry_md": state.get("feature_registry"),
            "spec_gap_context": json.loads(state.get("_spec_gap_context", "null")),
        }

        if reprompt is not None:
            payload["reprompt"] = reprompt.model_dump()

        return json.dumps(payload, ensure_ascii=False)
