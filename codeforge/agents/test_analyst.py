"""
agents/test_analyst.py — Test analyst agent.

Interprets test runner results. Classifies failures as:
  code_bug, test_bug, spec_gap, ambiguous, or environment error.

For fail_code_bug: recommended_action must be in behavioural terms only —
no test code, no stack traces. The CodeBugFinding whitelist in the orchestrator
enforces this structurally; the system prompt enforces it behaviourally.

Firewall: separate from test designer and coder.
Must never receive source code or coder's reasoning.
"""

from __future__ import annotations

import json
from typing import Any

from codeforge.agents.base import BaseAgent
from codeforge.firewall.assembler import ContextPackage
from codeforge.schemas.contracts import RePromptContext


class TestAnalystAgent(BaseAgent):
    """
    Formats the user turn for the test analyst.

    Input context:
      - requirements_doc (from artifacts)
      - code_artifact (from artifacts — to verify import style when classifying failures)
      - test_suite (from artifacts)
      - test_runner_results (injected by orchestrator after test execution)
      - test_coverage_map_md (from state_documents)

    Does NOT receive architecture_doc or coder reasoning.
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
            "code_artifact": artifacts["code_artifact"].model_dump()
            if "code_artifact" in artifacts else None,
            "test_suite": artifacts["test_suite"].model_dump()
            if "test_suite" in artifacts else None,
            # test_runner_results are injected by the orchestrator into state_documents
            # under the special key _test_runner_results
            "test_runner_results": json.loads(
                state.get("_test_runner_results", "null")
            ),
            "test_coverage_map_md": state.get("test_coverage_map", ""),
        }

        if reprompt is not None:
            payload["reprompt"] = reprompt.model_dump()

        return json.dumps(payload, ensure_ascii=False)
