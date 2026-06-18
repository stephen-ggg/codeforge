"""
agents/security_reviewer.py — Security reviewer agent.

Independent security pass: injection, auth, secrets, dependencies.
critical severity finding forces verdict: fail (D9 rule, enforced by orchestrator gates).
Different model recommended. Completely isolated from code reviewer.
Firewall: reads tech_stack only (not architecture_doc).
"""

from __future__ import annotations

import json
from typing import Any

from codeforge.agents.base import BaseAgent
from codeforge.firewall.assembler import ContextPackage
from codeforge.schemas.contracts import RePromptContext


class SecurityReviewerAgent(BaseAgent):
    """
    Formats the user turn for the security reviewer.

    Input context:
      - requirements_doc (from artifacts)
      - code_artifact (from artifacts)
      - tech_stack_md (from state_documents)

    Does NOT receive architecture_doc (firewall-enforced).
    Does NOT receive review_report (never sees code reviewer's reasoning).
    """

    def build_user_turn(
        self,
        context_package: ContextPackage,
        reprompt: RePromptContext | None = None,
    ) -> str:
        state = context_package.state_documents
        artifacts = context_package.artifacts

        payload: dict[str, Any] = {
            "stack_guidance": state.get("_stack_guidance", ""),
            "tech_stack_md": state.get("tech_stack", ""),
            "requirements_doc": artifacts["requirements_doc"].model_dump()
            if "requirements_doc" in artifacts else None,
            "code_artifact": artifacts["code_artifact"].model_dump()
            if "code_artifact" in artifacts else None,
        }

        if reprompt is not None:
            payload["reprompt"] = reprompt.model_dump()

        return json.dumps(payload, ensure_ascii=False)
