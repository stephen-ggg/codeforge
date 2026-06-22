"""
agents/requirements_analyst.py — Requirements analyst agent.

Converts a human brief into a structured RequirementsDoc with acceptance criteria.
The only agent that communicates with humans.
Emits either RequirementsNeedsClarification or RequirementsComplete.
"""

from __future__ import annotations

import json
from typing import Any

from codeforge.agents.base import BaseAgent
from codeforge.firewall.assembler import ContextPackage
from codeforge.schemas.contracts import RePromptContext


class RequirementsAnalystAgent(BaseAgent):
    """
    Formats the user turn for the requirements analyst.

    Input context:
      - human_brief (injected by orchestrator, not from artifact store)
      - clarification_history (prior rounds, if any)
      - confirm_rejection (if human rejected a prior requirements doc)
      - project_state: architecture_md, tech_stack_md, feature_registry_md,
        decisions_log_md, assumptions_log_md (from state_documents)
    """

    def build_user_turn(
        self,
        context_package: ContextPackage,
        reprompt: RePromptContext | None = None,
    ) -> str:
        # The orchestrator injects run-specific fields into context_package.state_documents
        # under special keys. Extract them here.
        state = context_package.state_documents

        payload: dict[str, Any] = {
            "run_mode": state.get("_run_mode", "new_project"),
            "human_brief": state.get("_human_brief", ""),
            "clarification_history": json.loads(state.get("_clarification_history", "[]")),
            "confirm_rejection": json.loads(state.get("_confirm_rejection", "null")),
            "project_state": {
                "architecture_md": state.get("architecture"),
                "tech_stack_md": state.get("tech_stack"),
                "feature_registry_md": state.get("feature_registry"),
                "decisions_log_md": state.get("decisions_log"),
                "assumptions_log_md": state.get("assumptions_log"),
                "requirements_summary": None,
            },
            "ui_design_md": state.get("ui_design"),
        }

        if reprompt is not None:
            payload["reprompt"] = reprompt.model_dump()

        return json.dumps(payload, ensure_ascii=False)
