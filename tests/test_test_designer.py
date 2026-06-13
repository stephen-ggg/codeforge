"""
Test designer anti-cheat tests.

Guards the defence-in-depth check: even if the assembler fails and code_artifact
reaches the test designer's context package, build_user_turn() must return a
synthetic output with a severity=block flag (FIREWALL-001) rather than leaking
source code to the LLM.
"""
from __future__ import annotations

import json

from codeforge.agents.test_designer import TestDesignerAgent
from codeforge.firewall.assembler import ContextPackage
from codeforge.schemas.contracts import AgentOutput


def test_block_flag_when_code_artifact_present() -> None:
    pkg = ContextPackage(agent_id="test_designer", run_id="run-test", assembly_id="asm-1")
    pkg.artifacts["code_artifact"] = AgentOutput(
        output={},
        assumptions_made=[],
        confidence=0.9,
        unresolved_flags=[],
    )

    # router and config are not used — build_user_turn short-circuits on the block path
    agent = TestDesignerAgent("test_designer", router=None, config=None)  # type: ignore[arg-type]
    raw = agent.build_user_turn(pkg, reprompt=None)

    result = json.loads(raw)
    flags = result["unresolved_flags"]
    assert len(flags) == 1
    assert flags[0]["id"] == "FIREWALL-001"
    assert flags[0]["severity"] == "block"


def _env_fix_context() -> dict:
    return {
        "trigger": "test_error_environment",
        "test_summary": "pytest could not collect tests",
        "environment_findings": [
            {"recommended_action": "Add pytest-json-report>=1.5 to requirements-test.txt",
             "evidence": "pytest: unrecognized arguments --json-report"}
        ],
    }


def test_env_fix_context_surfaced_in_payload() -> None:
    pkg = ContextPackage(agent_id="test_designer", run_id="run-test", assembly_id="asm-1")
    pkg.artifacts["requirements_doc"] = AgentOutput(
        output={}, assumptions_made=[], confidence=0.9, unresolved_flags=[],
    )
    pkg.state_documents["_env_fix_context"] = json.dumps(_env_fix_context())

    agent = TestDesignerAgent("test_designer", router=None, config=None)  # type: ignore[arg-type]
    payload = json.loads(agent.build_user_turn(pkg, reprompt=None))

    assert payload["env_fix_context"]["trigger"] == "test_error_environment"
    assert "pytest-json-report" in (
        payload["env_fix_context"]["environment_findings"][0]["recommended_action"]
    )


def test_firewall_block_wins_even_with_env_fix_context() -> None:
    """Env-fix re-entry must never weaken the anti-cheat barrier: if code_artifact is
    (wrongly) present, the FIREWALL-001 block path still fires regardless of env_fix_context."""
    pkg = ContextPackage(agent_id="test_designer", run_id="run-test", assembly_id="asm-1")
    pkg.artifacts["code_artifact"] = AgentOutput(
        output={}, assumptions_made=[], confidence=0.9, unresolved_flags=[],
    )
    pkg.state_documents["_env_fix_context"] = json.dumps(_env_fix_context())

    agent = TestDesignerAgent("test_designer", router=None, config=None)  # type: ignore[arg-type]
    result = json.loads(agent.build_user_turn(pkg, reprompt=None))

    assert result["unresolved_flags"][0]["id"] == "FIREWALL-001"
    assert "env_fix_context" not in result  # synthetic block output, no leaked context
