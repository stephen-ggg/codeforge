"""
Coder agent context tests — the runtime-dependency auto-recovery re-entry.
"""
from __future__ import annotations

import json

from codeforge.agents.coder import CoderAgent
from codeforge.firewall.assembler import ContextPackage


def test_dep_fix_context_surfaced_in_payload() -> None:
    pkg = ContextPackage(agent_id="coder", run_id="run-test", assembly_id="asm-1")
    pkg.state_documents["_dep_fix_context"] = json.dumps({
        "trigger": "runtime_dep_error",
        "error_phase": "runtime_dep_install_failed",
        "stderr_tail": "ERROR: No matching distribution found for leftpad==9.9",
    })

    agent = CoderAgent("coder", router=None, config=None)  # type: ignore[arg-type]
    payload = json.loads(agent.build_user_turn(pkg, reprompt=None))

    assert payload["dep_fix_context"]["trigger"] == "runtime_dep_error"
    assert "leftpad" in payload["dep_fix_context"]["stderr_tail"]
    # Absent by default (other coding paths don't set it).
    pkg2 = ContextPackage(agent_id="coder", run_id="run-test", assembly_id="asm-2")
    payload2 = json.loads(agent.build_user_turn(pkg2, reprompt=None))
    assert payload2["dep_fix_context"] is None
