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
