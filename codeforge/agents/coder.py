"""
agents/coder.py — Coder agent.

Implements from the architecture spec. Must emit the stack's dependency manifest at the
repo root (requirements.txt for python, package.json for nextjs-supabase) — the filename
comes from the active stack profile and is enforced by the coder validation gate.
Never receives test files. On fail_code_bug re-entry receives code_fix_context
with flagged AC ids only — no test content.
"""

from __future__ import annotations

import json
from typing import Any

from codeforge.agents.base import BaseAgent
from codeforge.firewall.assembler import ContextPackage
from codeforge.schemas.contracts import RePromptContext


class CoderAgent(BaseAgent):
    """
    Formats the user turn for the coder.

    Input context:
      - requirements_doc (from artifacts)
      - architecture_doc (from artifacts)
      - tech_stack_md (from state_documents)
      - existing_interfaces (injected by orchestrator from feature_registry)
      - retry_context (injected by orchestrator on review failure)
      - code_fix_context (injected by orchestrator on fail_code_bug re-entry)
      - dep_fix_context (injected on runtime-dependency error auto-recovery re-entry)
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

        architecture_doc = None
        if "architecture_doc" in artifacts:
            architecture_doc = artifacts["architecture_doc"].model_dump()

        payload: dict[str, Any] = {
            "run_mode": state.get("_run_mode", "new_project"),
            "stack_guidance": state.get("_stack_guidance", ""),
            "requirements_doc": requirements_doc,
            "architecture_doc": architecture_doc,
            "tech_stack_md": state.get("tech_stack"),
            "existing_interfaces": json.loads(state.get("_existing_interfaces", "[]")),
            "retry_context": json.loads(state.get("_retry_context", "null")),
            "code_fix_context": json.loads(state.get("_code_fix_context", "null")),
            "dep_fix_context": json.loads(state.get("_dep_fix_context", "null")),
            "ui_design_md": state.get("ui_design"),
        }

        if reprompt is not None:
            payload["reprompt"] = reprompt.model_dump()

        return json.dumps(payload, ensure_ascii=False)
