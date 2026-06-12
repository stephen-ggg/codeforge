"""
agents/test_designer.py — Test designer agent.

Writes tests from requirements + interface manifest ONLY.
Must never receive source code — this is the primary anti-cheat control.
The firewall assembler enforces this; this module must never add code_artifact
to the user turn even if it somehow appeared in the context package.
"""

from __future__ import annotations

import json
from typing import Any

from codeforge.agents.base import BaseAgent
from codeforge.firewall.assembler import ContextPackage
from codeforge.schemas.contracts import Flag, RePromptContext


# Sentinel flag raised if code somehow reaches this agent.
# The system prompt also instructs the model to flag a block in this case.
_CODE_LEAKED_FLAG = Flag(
    id="FIREWALL-001",
    description=(
        "code_artifact was present in the test designer context package. "
        "This is a firewall violation. The test designer must never see source code."
    ),
    severity="block",
    suggested_action="Halt pipeline immediately and audit the firewall assembler.",
)


class TestDesignerAgent(BaseAgent):
    """
    Formats the user turn for the test designer.

    Input context:
      - requirements_doc (from artifacts)
      - interface_manifest (from artifacts — orchestrator projection, not full arch_doc)
      - test_coverage_map_md (from state_documents)
      - feature_registry_md (from state_documents)
      - retry_context (injected on fail_test_bug loop)
      - code_fix_context (injected on fail_code_bug re-entry)

    CRITICAL: code_artifact must NEVER appear in the payload.
    This method explicitly excludes it and raises a block flag if detected.
    """

    def build_user_turn(
        self,
        context_package: ContextPackage,
        reprompt: RePromptContext | None = None,
    ) -> str:
        state = context_package.state_documents
        artifacts = context_package.artifacts

        # ANTI-CHEAT: detect if code_artifact somehow reached this agent
        # The assembler should have prevented this, but we check again as defence in depth.
        if "code_artifact" in artifacts:
            # Raise a block flag — this will halt the pipeline when the orchestrator
            # reads the output. We cannot prevent the LLM call at this point
            # (the check happens during user turn construction, before invocation)
            # so we embed the flag in a synthetic error output instead.
            import sys
            print(
                "FIREWALL VIOLATION: code_artifact present in test_designer context. "
                "This must never happen. Halting.",
                file=sys.stderr,
            )
            # Return a synthetic output that will trigger the block flag gate
            return json.dumps({
                "output": {
                    "test_cases": [],
                    "test_infrastructure": [],
                    "coverage_map": [],
                },
                "assumptions_made": [],
                "confidence": 0.0,
                "unresolved_flags": [_CODE_LEAKED_FLAG.model_dump()],
            }, ensure_ascii=False)

        payload: dict[str, Any] = {
            "requirements_doc": artifacts["requirements_doc"].model_dump()
            if "requirements_doc" in artifacts else None,
            "interface_manifest": artifacts["interface_manifest"].model_dump()
            if "interface_manifest" in artifacts else None,
            "test_coverage_map_md": state.get("test_coverage_map", ""),
            "feature_registry_md": state.get("feature_registry", ""),
            "retry_context": json.loads(state.get("_retry_context", "null")),
            "code_fix_context": json.loads(state.get("_code_fix_context", "null")),
        }

        if reprompt is not None:
            payload["reprompt"] = reprompt.model_dump()

        return json.dumps(payload, ensure_ascii=False)
