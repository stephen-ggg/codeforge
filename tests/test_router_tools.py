"""
Router tool-loop tests.

The loop must: drive call -> tool_use -> tool_result -> final answer, invoke the
executor for each tool_use, collect one tool_calls entry per inner model call,
and stop with the final JSON. The whole loop is one agent invocation, so the
orchestrator's call ceiling is unaffected (verified indirectly: complete()
returns a single RouterResult).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from codeforge.config.config_loader import ConfigSnapshot
from codeforge.model_router import router as router_mod
from codeforge.model_router.router import ModelRouter


def _msg(content, tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None)


def _response(message, call_id):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        _hidden_params={"litellm_call_id": call_id},
        id=call_id,
        usage=None,
    )


def _tool_call(call_id, name, args_json):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=args_json))


class FakeExecutor:
    max_tool_turns = 12

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name, args, litellm_call_id=None):
        self.calls.append((name, args))
        return "FILE CONTENTS: def add(a, b): ..."


def test_tool_loop_runs_then_returns_final_json(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    # Sequence: first completion asks for a tool, second returns final JSON.
    responses = [
        _response(_msg("", [_tool_call("tc1", "read_file", '{"path": "src/calc.py"}')]), "call-1"),
        _response(_msg('{"output": {"ok": true}, "confidence": 0.9}'), "call-2"),
    ]
    seen_kwargs: list[dict] = []

    def fake_completion(**kwargs):
        seen_kwargs.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)

    executor = FakeExecutor()
    router = ModelRouter(minimal_config)
    result = router.complete(
        agent_id="coder",
        system_prompt="sys",
        user_turn="add multiply",
        run_id="run-1",
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        tool_executor=executor,
    )

    # Executor was invoked for the tool_use block.
    assert executor.calls == [("read_file", {"path": "src/calc.py"})]
    # Final content is the JSON from the second call (thinking/tool prose stripped).
    assert result.content == '{"output": {"ok": true}, "confidence": 0.9}'
    # One tool_calls entry per inner model call (cost attribution survives).
    assert [c["litellm_call_id"] for c in result.tool_calls] == ["call-1", "call-2"]
    # The first request offered tools; after a tool result the loop continued.
    assert seen_kwargs[0]["tools"]
    # The tool_result was threaded back into the second request's messages.
    roles = [m["role"] for m in seen_kwargs[1]["messages"]]
    assert "tool" in roles


def test_no_tools_path_is_single_shot(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    calls = {"n": 0}

    def fake_completion(**kwargs):
        calls["n"] += 1
        assert "tools" not in kwargs
        return _response(_msg('{"output": {}, "confidence": 1.0}'), "solo")

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)

    router = ModelRouter(minimal_config)
    result = router.complete(agent_id="coder", system_prompt="s", user_turn="u", run_id="r")

    assert calls["n"] == 1
    assert result.tool_calls == []
    assert result.content == '{"output": {}, "confidence": 1.0}'
