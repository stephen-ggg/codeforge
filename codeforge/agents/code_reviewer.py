"""
agents/code_reviewer.py — Code reviewer agent.

Reviews for correctness, clarity, adherence to spec.
error severity finding forces verdict: fail (D9 rule, enforced by orchestrator gates).
Firewall: blind to coder's system prompt, prior reviewer outputs, and test files.
"""

from __future__ import annotations

import json
from typing import Any

from codeforge.agents.base import BaseAgent
from codeforge.firewall.assembler import ContextPackage
from codeforge.schemas.contracts import RePromptContext


class CodeReviewerAgent(BaseAgent):
    """
    Formats the user turn for the code reviewer.

    Input context:
      - requirements_doc (from artifacts)
      - architecture_doc (from artifacts)
      - code_artifact (from artifacts)
      - decisions_log_md (from state_documents)
    """

    def build_user_turn(
        self,
        context_package: ContextPackage,
        reprompt: RePromptContext | None = None,
    ) -> str:
        state = context_package.state_documents
        artifacts = context_package.artifacts

        payload: dict[str, Any] = {
            "requirements_doc": artifacts["requirements_doc"].model_dump()
            if "requirements_doc" in artifacts else None,
            "architecture_doc": artifacts["architecture_doc"].model_dump()
            if "architecture_doc" in artifacts else None,
            "code_artifact": artifacts["code_artifact"].model_dump()
            if "code_artifact" in artifacts else None,
            "decisions_log_md": state.get("decisions_log", ""),
        }

        if reprompt is not None:
            payload["reprompt"] = reprompt.model_dump()

        return json.dumps(payload, ensure_ascii=False)
