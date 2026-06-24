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


def _chunk(content=None, reasoning_content=None, finish_reason=None,
           call_id=None, usage=None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning_content)
    chunk = SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)],
        usage=None,
        id=call_id,
    )
    # Last chunk carries usage in _hidden_params; litellm_call_id is absent on stream.
    chunk._hidden_params = {"usage": usage} if usage is not None else {}
    return chunk


def test_streaming_agent_accumulates_chunks(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    # test_analyst has streaming: true in the shipped config.
    usage_obj = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    chunks = [
        _chunk(reasoning_content="thinking part 1 "),
        _chunk(reasoning_content="thinking part 2"),
        _chunk(content='{"output": {"ok": '),
        _chunk(content='true}, "confidence": 0.9}'),
        _chunk(finish_reason="stop", call_id="resp-id-123", usage=usage_obj),
    ]
    seen_kwargs: list[dict] = []

    def fake_completion(**kwargs):
        seen_kwargs.append(kwargs)
        assert kwargs.get("stream") is True
        return iter(chunks)

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)

    router = ModelRouter(minimal_config)
    result = router.complete(agent_id="test_analyst", system_prompt="s", user_turn="u", run_id="r")

    assert result.content == '{"output": {"ok": true}, "confidence": 0.9}'
    assert result.thinking == "thinking part 1 thinking part 2"
    # call id falls back to the provider response id (chunk.id) on streaming.
    assert result.litellm_call_id == "resp-id-123"
    # usage extracted from the last chunk's _hidden_params Usage object.
    assert result.usage == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert result.truncated is False


def test_streaming_marks_truncated_on_length(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    chunks = [
        _chunk(content='{"output": {"ok": true'),
        _chunk(finish_reason="length", call_id="resp-trunc"),
    ]

    monkeypatch.setattr(router_mod.litellm, "completion", lambda **kw: iter(chunks))

    router = ModelRouter(minimal_config)
    result = router.complete(agent_id="test_analyst", system_prompt="s", user_turn="u", run_id="r")

    assert result.truncated is True


def test_streaming_missing_finish_reason_marks_truncated(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    """A stream cut after ≥1 chunk that ends WITHOUT a terminal finish_reason (None)
    is partial — it must be flagged truncated even though the provider never said
    'length'. Otherwise a network-cut response masquerades as complete."""
    chunks = [
        _chunk(content='{"output": {"ok": true}, "confidence": 0.9}'),
        _chunk(finish_reason=None, call_id="resp-cut"),
    ]
    monkeypatch.setattr(router_mod.litellm, "completion", lambda **kw: iter(chunks))

    router = ModelRouter(minimal_config)
    result = router.complete(agent_id="test_analyst", system_prompt="s", user_turn="u", run_id="r")

    assert result.truncated is True


def test_streaming_truncated_does_not_salvage_quoted_envelope(
    monkeypatch, minimal_config: ConfigSnapshot
) -> None:
    """The dangerous case: reasoning quotes a complete envelope-shaped object, then the
    REAL final output is cut off mid-JSON. A truncated response must not return the
    salvaged earlier object as if it were clean — content must fail structural parse so
    the orchestrator routes it through the truncation path."""
    quoted = (
        '{"output": {"example": 1}, "assumptions_made": [], '
        '"confidence": 0.5, "unresolved_flags": []}'
    )
    chunks = [
        _chunk(content=f"Here is the schema I will follow: {quoted}\nNow the answer: "),
        _chunk(content='{"output": {"real": tru'),   # cut off mid-token
        _chunk(finish_reason="length", call_id="resp-trunc2"),
    ]
    monkeypatch.setattr(router_mod.litellm, "completion", lambda **kw: iter(chunks))

    router = ModelRouter(minimal_config)
    result = router.complete(agent_id="test_analyst", system_prompt="s", user_turn="u", run_id="r")

    assert result.truncated is True
    # The salvaged quoted envelope must NOT be returned as the clean answer.
    assert result.content != quoted
    # Content does not parse as a single JSON object → structural gate rejects it.
    import json as _json
    with pytest.raises(_json.JSONDecodeError):
        _json.loads(result.content)


def test_streaming_skipped_when_tools_present(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    # A streaming agent invoked with tools must NOT stream (tool_calls fragment
    # across chunks); it falls back to the non-streaming tool loop.
    def fake_completion(**kwargs):
        assert "stream" not in kwargs
        return _response(_msg('{"output": {}, "confidence": 1.0}'), "no-stream")

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)

    executor = FakeExecutor()
    router = ModelRouter(minimal_config)
    result = router.complete(
        agent_id="test_analyst",
        system_prompt="s",
        user_turn="u",
        run_id="r",
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        tool_executor=executor,
    )

    assert result.content == '{"output": {}, "confidence": 1.0}'


def test_no_tools_path_is_single_shot(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    calls = {"n": 0}

    def fake_completion(**kwargs):
        calls["n"] += 1
        assert "tools" not in kwargs
        if kwargs.get("stream"):
            return iter([
                _chunk(content='{"output": {}, "confidence": 1.0}'),
                _chunk(finish_reason="stop", call_id="solo"),
            ])
        return _response(_msg('{"output": {}, "confidence": 1.0}'), "solo")

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)

    router = ModelRouter(minimal_config)
    result = router.complete(agent_id="coder", system_prompt="s", user_turn="u", run_id="r")

    assert calls["n"] == 1
    assert result.tool_calls == []
    assert result.content == '{"output": {}, "confidence": 1.0}'


# ---------------------------------------------------------------------------
# Exponential backoff tests
# ---------------------------------------------------------------------------

from litellm.exceptions import (  # noqa: E402
    AuthenticationError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
)

from codeforge.model_router.router import RouterError


def _config_with_fallback(base: ConfigSnapshot, agent_id: str, fallback: str) -> ConfigSnapshot:
    agents = dict(base.agents)
    agents[agent_id] = agents[agent_id].model_copy(update={"fallback_model": fallback})
    return base.model_copy(update={"agents": agents})


def test_backoff_retries_transient_then_succeeds(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    """Two ServiceUnavailableError calls followed by success — 3 calls total, 2 sleeps.

    Succeeds on attempt 3 (well within _MAX_ATTEMPTS) so budget is not relevant here.
    """
    # Use code_reviewer: non-streaming, so _response() is the right return shape.
    call_count = {"n": 0}
    sleep_calls: list[float] = []

    def fake_completion(**kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            raise ServiceUnavailableError(
                message="overloaded", llm_provider="anthropic", model="claude-sonnet-4-6",
            )
        return _response(_msg('{"output": {}, "confidence": 0.9}'), "ok")

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(router_mod.time, "sleep", lambda s: sleep_calls.append(s))

    router = ModelRouter(minimal_config)
    result = router.complete(agent_id="code_reviewer", system_prompt="s", user_turn="u", run_id="r")

    assert call_count["n"] == 3
    assert len(sleep_calls) == 2
    assert sleep_calls[0] < sleep_calls[1]  # delays grow
    assert result.content == '{"output": {}, "confidence": 0.9}'


def test_non_transient_error_skips_backoff(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    """AuthenticationError is not transient — must raise immediately with no sleep."""
    call_count = {"n": 0}
    sleep_calls: list[float] = []

    def fake_completion(**kwargs):
        call_count["n"] += 1
        raise AuthenticationError(
            message="invalid key", llm_provider="anthropic", model="claude-sonnet-4-6",
        )

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(router_mod.time, "sleep", lambda s: sleep_calls.append(s))

    router = ModelRouter(minimal_config)
    with pytest.raises(RouterError):
        router.complete(agent_id="code_reviewer", system_prompt="s", user_turn="u", run_id="r")

    assert call_count["n"] == 1
    assert sleep_calls == []


def test_rate_limit_error_is_retried(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    """RateLimitError (429) is in the transient set and must trigger backoff."""
    call_count = {"n": 0}

    def fake_completion(**kwargs):
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise RateLimitError(
                message="rate limit", llm_provider="anthropic", model="claude-sonnet-4-6",
            )
        return _response(_msg('{"output": {}, "confidence": 0.9}'), "ok")

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(router_mod.time, "sleep", lambda s: None)

    router = ModelRouter(minimal_config)
    result = router.complete(agent_id="code_reviewer", system_prompt="s", user_turn="u", run_id="r")

    assert call_count["n"] == 2
    assert result.content == '{"output": {}, "confidence": 0.9}'


def test_retry_after_header_honored(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    """When _retry_after() returns a value larger than the computed backoff, it wins."""
    sleep_calls: list[float] = []
    call_count = {"n": 0}

    def fake_completion(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RateLimitError(
                message="rate limit", llm_provider="anthropic", model="claude-sonnet-4-6",
            )
        return _response(_msg('{"output": {}, "confidence": 0.9}'), "ok")

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(router_mod.time, "sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr(router_mod, "_retry_after", lambda exc: 30.0)

    router = ModelRouter(minimal_config)
    result = router.complete(agent_id="code_reviewer", system_prompt="s", user_turn="u", run_id="r")

    assert call_count["n"] == 2
    assert len(sleep_calls) == 1
    assert sleep_calls[0] >= 30.0  # Retry-After value overrides computed exponential delay
    assert result.content == '{"output": {}, "confidence": 0.9}'


def test_internal_server_error_is_retried(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    """InternalServerError (500) is in the transient set and must trigger backoff."""
    call_count = {"n": 0}

    def fake_completion(**kwargs):
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise InternalServerError(
                message="internal error", llm_provider="anthropic", model="claude-sonnet-4-6",
            )
        return _response(_msg('{"output": {}, "confidence": 0.9}'), "ok")

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(router_mod.time, "sleep", lambda s: None)

    router = ModelRouter(minimal_config)
    result = router.complete(agent_id="code_reviewer", system_prompt="s", user_turn="u", run_id="r")

    assert call_count["n"] == 2
    assert result.content == '{"output": {}, "confidence": 0.9}'


def test_backoff_exhausted_falls_to_fallback_model(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    """Primary exhausts all _MAX_ATTEMPTS retries with ServiceUnavailableError; fallback succeeds."""
    # code_reviewer (claude-sonnet-4-6) → fallback haiku; both are non-streaming.
    config = _config_with_fallback(minimal_config, "code_reviewer", "claude-haiku-4-5-20251001")
    primary_calls = {"n": 0}
    fallback_calls = {"n": 0}
    sleep_calls: list[float] = []

    def fake_completion(**kwargs):
        model = kwargs.get("model", "")
        if "sonnet" in model:
            primary_calls["n"] += 1
            raise ServiceUnavailableError(
                message="overloaded", llm_provider="anthropic", model=model,
            )
        else:
            fallback_calls["n"] += 1
            return _response(_msg('{"output": {}, "confidence": 0.7}'), "fb-ok")

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(router_mod.time, "sleep", lambda s: sleep_calls.append(s))

    router = ModelRouter(config)
    result = router.complete(agent_id="code_reviewer", system_prompt="s", user_turn="u", run_id="r")

    assert primary_calls["n"] == router_mod._MAX_ATTEMPTS
    assert fallback_calls["n"] == 1
    assert len(sleep_calls) == router_mod._MAX_ATTEMPTS - 1
    assert result.model_used == "claude-haiku-4-5-20251001"
    assert result.content == '{"output": {}, "confidence": 0.7}'


def test_backoff_all_exhausted_raises_router_error(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    """Both primary and fallback exhaust retries — RouterError raised."""
    config = _config_with_fallback(minimal_config, "code_reviewer", "claude-haiku-4-5-20251001")
    call_count = {"n": 0}

    def fake_completion(**kwargs):
        call_count["n"] += 1
        raise ServiceUnavailableError(
            message="overloaded", llm_provider="anthropic", model=kwargs.get("model", ""),
        )

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(router_mod.time, "sleep", lambda s: None)

    router = ModelRouter(config)
    with pytest.raises(RouterError):
        router.complete(agent_id="code_reviewer", system_prompt="s", user_turn="u", run_id="r")

    assert call_count["n"] == router_mod._MAX_ATTEMPTS * 2


def test_tool_loop_backoff_per_individual_call(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    """Backoff applies per litellm.completion() call inside the tool loop.

    Turn 1: fails twice (transient), then succeeds returning a tool_use.
    Turn 2: succeeds immediately returning final JSON.
    Expected: 4 total completion calls, 2 sleeps, executor called once.
    """
    call_count = {"n": 0}
    sleep_calls: list[float] = []

    def fake_completion(**kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 2:
            raise ServiceUnavailableError(
                message="overloaded", llm_provider="anthropic", model="claude-opus-4-8",
            )
        if call_count["n"] == 3:
            return _response(
                _msg("", [_tool_call("tc1", "read_file", '{"path": "x.py"}')]), "t1",
            )
        return _response(_msg('{"output": {}, "confidence": 0.9}'), "t2")

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(router_mod.time, "sleep", lambda s: sleep_calls.append(s))

    executor = FakeExecutor()
    router = ModelRouter(minimal_config)
    result = router.complete(
        agent_id="coder",
        system_prompt="s",
        user_turn="u",
        run_id="r",
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        tool_executor=executor,
    )

    assert call_count["n"] == 4
    assert len(sleep_calls) == 2
    assert executor.calls == [("read_file", {"path": "x.py"})]
    assert result.content == '{"output": {}, "confidence": 0.9}'


def test_streaming_mid_iteration_error_not_retried(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    """ServiceUnavailableError raised mid-stream (after ≥1 chunk) is NOT retried.

    Retrying would re-request from token 0, re-billing the full token budget including
    expensive thinking tokens. The error propagates immediately instead.
    """
    call_count = {"n": 0}
    sleep_calls: list[float] = []

    def bad_stream():
        yield _chunk(content='{"partial":')
        raise ServiceUnavailableError(
            message="mid-stream drop", llm_provider="anthropic", model="claude-opus-4-8",
        )

    def fake_completion(**kwargs):
        call_count["n"] += 1
        return bad_stream()

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(router_mod.time, "sleep", lambda s: sleep_calls.append(s))

    router = ModelRouter(minimal_config)
    with pytest.raises(RouterError):
        router.complete(agent_id="test_analyst", system_prompt="s", user_turn="u", run_id="r")

    assert call_count["n"] == 1  # no retry after mid-stream failure
    assert sleep_calls == []


def test_tool_loop_retry_exhaustion_falls_to_forced_final(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    """Retry exhaustion mid-tool-loop breaks to forced-final with accumulated context.

    Turn 1: succeeds (returns tool_use). Turn 2: all retries fail. The loop breaks
    and the forced-final-answer call receives the accumulated messages including the
    tool result from turn 1.
    """
    call_count = {"n": 0}
    forced_final_messages: list[dict] = []

    def fake_completion(**kwargs):
        call_count["n"] += 1
        n = call_count["n"]
        if n == 1:
            return _response(
                _msg("", [_tool_call("tc1", "read_file", '{"path": "x.py"}')]), "t1",
            )
        if n <= 1 + router_mod._MAX_ATTEMPTS:
            raise ServiceUnavailableError(
                message="overloaded", llm_provider="anthropic", model="claude-opus-4-8",
            )
        forced_final_messages.extend(kwargs["messages"])
        return _response(_msg('{"output": {}, "confidence": 0.9}'), "final")

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(router_mod.time, "sleep", lambda s: None)

    executor = FakeExecutor()
    router = ModelRouter(minimal_config)
    result = router.complete(
        agent_id="coder",
        system_prompt="s",
        user_turn="u",
        run_id="r",
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        tool_executor=executor,
    )

    assert result.content == '{"output": {}, "confidence": 0.9}'
    # Forced-final received accumulated context including turn 1's tool result.
    roles = [m["role"] for m in forced_final_messages]
    assert "tool" in roles
    assert executor.calls == [("read_file", {"path": "x.py"})]
    # turn 1 (1) + turn 2 attempts (_MAX_ATTEMPTS) + forced-final (1)
    assert call_count["n"] == 1 + router_mod._MAX_ATTEMPTS + 1


# ---------------------------------------------------------------------------
# _extract_json unit tests
# ---------------------------------------------------------------------------

class TestExtractJson:
    """Direct unit tests for the static _extract_json method."""

    def test_pure_json_fast_path(self) -> None:
        payload = '{"output": {"ok": true}, "confidence": 0.9}'
        assert ModelRouter._extract_json(payload) == payload

    def test_markdown_fenced_json(self) -> None:
        payload = '```json\n{"output": {"ok": true}}\n```'
        assert ModelRouter._extract_json(payload) == '{"output": {"ok": true}}'

    def test_prose_with_balanced_braces_before_json(self) -> None:
        # Balanced prose braces (one `{` matched by one `}`) before the JSON.
        text = 'The selector `#id { color: red; }` is forbidden.\n{"output": {"ok": true}}'
        assert ModelRouter._extract_json(text) == '{"output": {"ok": true}}'

    def test_prose_with_unmatched_open_brace_in_regex(self) -> None:
        # Exact failure pattern from run-ba324491e0f9: regex patterns in
        # backtick spans produce `{` without a following `}`.
        text = (
            'Use `/^\\s*#[a-zA-Z][a-zA-Z0-9_-]*\\s*[{,]/m` for selectors.\n'
            '{"output": {"ok": true}}'
        )
        assert ModelRouter._extract_json(text) == '{"output": {"ok": true}}'

    def test_prose_open_braces_outnumber_close_braces(self) -> None:
        # Three unmatched `{` in prose, zero matching `}` in prose.
        text = 'Patterns: `{`, `{id}`, `{n,m` — none closed.\n{"output": {"ok": true}}'
        assert ModelRouter._extract_json(text) == '{"output": {"ok": true}}'

    def test_json_with_embedded_code_strings_containing_braces(self) -> None:
        # The JSON itself contains TypeScript/CSS code as string values with braces.
        # The real lexer treats them as string content, not structure.
        code = "const x = { a: 1 }; function f() { return x; }"
        import json as _json
        payload = _json.dumps({"output": {"code": code}, "confidence": 0.9})
        text = "Reasoning prose here.\n" + payload
        assert ModelRouter._extract_json(text) == payload

    def test_json_with_escaped_quotes_in_strings(self) -> None:
        # Escaped `\"` inside a JSON string is handled by the real lexer.
        import json as _json
        payload = _json.dumps({"output": {"msg": 'say "hello"'}, "confidence": 1.0})
        text = "Some prose.\n" + payload
        assert ModelRouter._extract_json(text) == payload

    def test_returns_last_json_object_when_multiple_present(self) -> None:
        # If two top-level JSON objects appear (neither a full envelope), return the
        # last one — the contract puts the real output last.
        first = '{"output": {"v": 1}}'
        second = '{"output": {"v": 2}}'
        text = f"Old attempt: {first}\nNew attempt: {second}"
        assert ModelRouter._extract_json(text) == second

    def test_envelope_followed_by_trailing_snippet_returns_envelope(self) -> None:
        # Regression: run-097cfe57faf8 / run-1090a5aa6337. The model emitted a full
        # envelope and then appended a standalone TestCase/CodeFile object. The old
        # backward scan anchored on the trailing snippet's `}` and returned the
        # snippet (→ "4 missing required fields"). The envelope must win.
        import json as _json
        envelope = _json.dumps({
            "output": {"test_cases": [{"id": "TC-001"}]},
            "assumptions_made": [],
            "confidence": 0.9,
            "unresolved_flags": [],
        })
        snippet = _json.dumps({"path": "a.test.ts", "content": "x", "language": "ts"})
        text = f"Here are the tests.\n\n{envelope}\n\nExample of the last file:\n{snippet}"
        assert ModelRouter._extract_json(text) == envelope

    def test_envelope_followed_by_trailing_prose_returns_envelope(self) -> None:
        # A postscript after the JSON must not break extraction.
        import json as _json
        envelope = _json.dumps({
            "output": {"ok": True},
            "assumptions_made": [],
            "confidence": 1.0,
            "unresolved_flags": [],
        })
        text = f"{envelope}\n\nLet me know if you'd like any changes!"
        assert ModelRouter._extract_json(text) == envelope

    def test_envelope_preferred_over_decoy_json_in_reasoning(self) -> None:
        # The model quotes a JSON snippet in its reasoning, then emits the real
        # envelope. The envelope (carrying all four keys) must be selected even
        # though the decoy parses as valid JSON and appears first.
        import json as _json
        decoy = _json.dumps({"name": "foo", "type": "unit"})
        envelope = _json.dumps({
            "output": {"ok": True},
            "assumptions_made": [],
            "confidence": 0.8,
            "unresolved_flags": [],
        })
        text = f"The interface looks like {decoy} so I will test it.\n{envelope}"
        assert ModelRouter._extract_json(text) == envelope

    def test_bare_inner_object_without_envelope_returned_for_gate_to_reject(self) -> None:
        # When the model emits only a bare object (no envelope anywhere), surface it
        # unchanged so the schema_valid gate reports the real error instead of the
        # extractor masking it.
        bare = '{"id": "TC-007", "title": "x", "type": "unit"}'
        assert ModelRouter._extract_json(f"Reasoning.\n{bare}") == bare

    def test_empty_string_returned_as_is(self) -> None:
        assert ModelRouter._extract_json("") == ""

    def test_no_closing_brace_returned_as_is(self) -> None:
        text = 'This string has no closing brace {"oops":'
        # No `}` found — returns original stripped string for gate to reject.
        assert ModelRouter._extract_json(text) == text.strip()

    def test_whitespace_around_json_stripped(self) -> None:
        payload = '{"output": {"ok": true}}'
        assert ModelRouter._extract_json(f"\n  {payload}  \n") == payload


def test_streaming_transient_error_retried(monkeypatch, minimal_config: ConfigSnapshot) -> None:
    """ServiceUnavailableError on first stream creation triggers retry; second succeeds."""
    call_count = {"n": 0}
    sleep_calls: list[float] = []
    chunks = [
        _chunk(content='{"output": {}, "confidence": 0.9}'),
        _chunk(finish_reason="stop", call_id="stream-ok"),
    ]

    def fake_completion(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ServiceUnavailableError(
                message="overloaded", llm_provider="anthropic", model="claude-opus-4-8",
            )
        return iter(chunks)

    monkeypatch.setattr(router_mod.litellm, "completion", fake_completion)
    monkeypatch.setattr(router_mod.time, "sleep", lambda s: sleep_calls.append(s))

    router = ModelRouter(minimal_config)
    result = router.complete(agent_id="test_analyst", system_prompt="s", user_turn="u", run_id="r")

    assert call_count["n"] == 2
    assert len(sleep_calls) == 1
    assert result.content == '{"output": {}, "confidence": 0.9}'
