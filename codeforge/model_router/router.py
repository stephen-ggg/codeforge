"""
model_router/router.py — LiteLLM-based model router.

THIS IS THE ONLY FILE IN CODEFORGE THAT IMPORTS LITELLM.
Swapping the LLM provider library means editing this file only.

Responsibilities:
  - Translate a generic complete() call into the correct LiteLLM API call
  - Look up model, temperature, max_tokens, fallback_model, thinking from config
  - Enable Anthropic extended thinking when configured (claude-* models only);
    fall back to a <thinking> scratchpad instruction for non-Anthropic models
  - Normalise the response: strip any thinking block, extract the JSON text so the
    orchestrator always receives a clean JSON string regardless of provider
  - Stamp every call with agent_id, run_id, and codeforge metadata
  - Capture litellm_call_id as the authoritative cost-attribution identifier
  - On provider error: retry once with fallback_model if configured
  - Return RouterResult — extracted text content, raw thinking (for debug), call id,
    model used, token usage

The router does NOT validate response content against the schema. That is the
orchestrator's job. It only extracts the JSON-bearing text from the response envelope.

=== CHANGES FROM PRE-THINKING VERSION ===
  * _call now passes a `thinking={...}` param and forces temperature=1 when thinking
    is enabled on an Anthropic model.
  * Response handling moved into _extract_content(), which accepts both a plain string
    and a list of typed blocks (Anthropic thinking shape via LiteLLM).
  * complete() decides per-agent whether thinking is available; if configured-but-
    unavailable (non-Anthropic), it appends a scratchpad instruction to the user turn
    and the extractor strips the prose before the final JSON object.
  * RouterResult gains `thinking` (raw reasoning text, for raw_outputs/ debugging).
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import litellm  # noqa: PGH003
from litellm import exceptions as _litellm_exc

from codeforge.config.config_loader import ConfigSnapshot
from codeforge.schemas.contracts import AgentId

logger = logging.getLogger(__name__)

litellm.suppress_debug_info = True

# Models that do NOT accept a `temperature` parameter.
# Re-evaluate and re-add temperature support per model as the API evolves.
#
# Models that DO accept temperature: claude-opus-4-6 (and earlier Opus versions),
# claude-sonnet-4-6, claude-haiku-4-5-*, all claude-3-* models.
#
# Models that do NOT accept temperature (omitted from requests):
#   claude-opus-4-7, claude-opus-4-8
_MODELS_WITHOUT_TEMPERATURE: frozenset[str] = frozenset({
    "claude-opus-4-7",
    "claude-opus-4-8",
})


def _supports_temperature(model: str) -> bool:
    """Return False for models that reject the temperature parameter.

    Checks by substring so provider-prefixed variants (anthropic/claude-opus-4-8,
    us.anthropic.claude-opus-4-8) are also matched.
    """
    m = model.lower()
    return not any(no_temp in m for no_temp in _MODELS_WITHOUT_TEMPERATURE)


# Appended to the user turn ONLY when thinking is configured but the model can't do it
# natively (non-Anthropic provider). Anthropic models get a real thinking block instead.
_SCRATCHPAD_INSTRUCTION = (
    "\n\n---\n\n"
    "Before your output object, reason through the problem in plain text. "
    "When you are done reasoning, output the JSON object as the final thing in your "
    "response. The JSON object must be the last content you produce."
)

_MAX_ATTEMPTS: int = 5
_BACKOFF_BASE: float = 2.0

# The four top-level keys every agent's output envelope carries (mirrors
# schemas.contracts.AgentOutput). Used only to disambiguate which parsed object is
# the envelope when a response contains more than one JSON object — e.g. the model
# quoted a JSON snippet in its reasoning, or appended a trailing example. This is a
# selection hint, NOT schema validation (that remains the orchestrator's job).
_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {"output", "assumptions_made", "confidence", "unresolved_flags"}
)

# Transient errors safe to retry on the same model. Everything else
# (AuthenticationError, BadRequestError, PermissionDeniedError, etc.)
# propagates immediately so complete() can fall through to the fallback model.
_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    _litellm_exc.RateLimitError,           # 429
    _litellm_exc.ServiceUnavailableError,  # 503, Anthropic 529 overloaded
    _litellm_exc.InternalServerError,      # 500
    _litellm_exc.BadGatewayError,          # 502
    _litellm_exc.APIConnectionError,       # network failures
    _litellm_exc.Timeout,                  # read/connect timeouts
)


def _retry_after(exc: Exception) -> float:
    """Return the Retry-After delay in seconds from a litellm exception, or 0."""
    response = getattr(exc, "response", None)
    if response is None:
        return 0.0
    headers = getattr(response, "headers", {}) or {}
    val = headers.get("Retry-After") or headers.get("retry-after")
    if val is None:
        return 0.0
    try:
        return max(0.0, float(val))
    except (ValueError, TypeError):
        return 0.0


def _with_backoff(
    fn: Callable[[], Any],
    *,
    agent_id: str,
    model: str,
    max_attempts: int = _MAX_ATTEMPTS,
    backoff_base: float = _BACKOFF_BASE,
) -> Any:
    """Call fn(), retrying on transient litellm errors with exponential backoff + jitter.

    Non-transient errors propagate immediately without sleeping.
    On exhaustion re-raises the last transient error so complete() can fall to fallback.
    Honors Retry-After headers when present (e.g. on 429 rate-limit responses).
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    for attempt in range(max_attempts):
        try:
            return fn()
        except _TRANSIENT_ERRORS as exc:
            if attempt == max_attempts - 1:
                raise
            computed = backoff_base * (2 ** attempt) + random.uniform(0, 0.2 * backoff_base)
            delay = max(computed, _retry_after(exc))
            logger.warning(
                "Router: agent=%s model=%s transient error (attempt %d/%d), "
                "retrying in %.2fs: %s",
                agent_id, model, attempt + 1, max_attempts, delay, exc,
            )
            time.sleep(delay)


@dataclass
class RouterResult:
    """Result of a completion call (or a tool loop that ends in a completion)."""
    content: str                            # extracted JSON-bearing text (thinking stripped)
    litellm_call_id: str                    # authoritative cost-attribution identifier
    model_used: str                         # actual model that responded (may be fallback)
    thinking: str | None = None             # raw reasoning, for raw_outputs/ — not validated
    usage: dict[str, Any] = field(default_factory=dict)
    # One entry per inner model call when a tool loop ran (else empty). Each entry
    # carries the litellm_call_id + usage so cost attribution survives the loop.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False                  # finish_reason == "length": output hit max_tokens


class RouterError(Exception):
    """Raised when the router cannot get a response after primary + fallback attempts."""

    def __init__(self, agent_id: AgentId, model: str, cause: Exception) -> None:
        self.agent_id = agent_id
        self.model = model
        self.cause = cause
        super().__init__(
            f"Router failed for agent '{agent_id}' on model '{model}': {cause}"
        )


def _supports_native_thinking(model: str) -> bool:
    """Extended thinking is an Anthropic feature. LiteLLM model strings for Anthropic
    start with 'claude-' (or 'anthropic/')."""
    m = model.lower()
    return m.startswith("claude-") or m.startswith("anthropic/")


class ModelRouter:
    """Routes agent completion requests through LiteLLM. One instance per run."""

    def __init__(self, config: ConfigSnapshot) -> None:
        self._config = config

    def complete(
        self,
        agent_id: AgentId,
        system_prompt: str,
        user_turn: str,
        run_id: str,
        tools: list[dict[str, Any]] | None = None,
        tool_executor: Any | None = None,
    ) -> RouterResult:
        """Call the configured LLM for agent_id and return a normalised result.

        When `tools` and `tool_executor` are supplied (continuation runs, tool-
        enabled agents), the call runs a read-only tool loop before producing the
        final JSON artifact; otherwise it is a single completion as before.

        Raises:
            RouterError: if both primary and fallback calls fail.
        """
        agent_config = self._config.agents.get(agent_id)
        if agent_config is None:
            raise RouterError(
                agent_id, "<unknown>",
                ValueError(f"No config found for agent '{agent_id}'"),
            )

        metadata = {
            "agent_id": agent_id,
            "run_id": run_id,
            "name": self._config.name,
            "codeforge_version": self._config.name,
        }

        thinking_cfg = getattr(agent_config, "thinking", None)
        want_thinking = bool(thinking_cfg and thinking_cfg.enabled)
        native = want_thinking and _supports_native_thinking(agent_config.model)

        # Non-native providers that still want reasoning get the scratchpad instruction.
        effective_user_turn = user_turn
        if want_thinking and not native:
            logger.info(
                "Agent '%s' has thinking enabled but model '%s' is not Anthropic; "
                "using scratchpad fallback.", agent_id, agent_config.model,
            )
            effective_user_turn = user_turn + _SCRATCHPAD_INSTRUCTION

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": effective_user_turn},
        ]

        thinking_param = (
            {"type": "enabled", "budget_tokens": thinking_cfg.budget_tokens}
            if native and thinking_cfg is not None else None
        )

        try:
            return self._call(
                model=agent_config.model,
                messages=messages,
                temperature=agent_config.temperature,
                max_tokens=agent_config.max_tokens,
                metadata=metadata,
                agent_id=agent_id,
                thinking_param=thinking_param,
                tools=tools,
                tool_executor=tool_executor,
            )
        except Exception as primary_exc:
            logger.warning(
                "Primary model failed for agent '%s' (model: %s): %s",
                agent_id, agent_config.model, primary_exc,
            )
            if not agent_config.fallback_model:
                raise RouterError(agent_id, agent_config.model, primary_exc) from primary_exc

            # Fallback: thinking availability is recomputed for the fallback model.
            fb_native = want_thinking and _supports_native_thinking(agent_config.fallback_model)
            fb_messages = messages
            fb_thinking = (
                {"type": "enabled", "budget_tokens": thinking_cfg.budget_tokens}
                if fb_native and thinking_cfg is not None else None
            )
            if want_thinking and not fb_native and not native:
                # already added scratchpad; leave as-is
                pass
            elif want_thinking and not fb_native and native:
                # primary was native (no scratchpad) but fallback isn't — add it now
                fb_messages = [
                    messages[0],
                    {"role": "user", "content": user_turn + _SCRATCHPAD_INSTRUCTION},
                ]

            logger.info("Retrying agent '%s' with fallback '%s'", agent_id, agent_config.fallback_model)
            try:
                return self._call(
                    model=agent_config.fallback_model,
                    messages=fb_messages,
                    temperature=agent_config.temperature,
                    max_tokens=agent_config.max_tokens,
                    metadata=metadata,
                    agent_id=agent_id,
                    thinking_param=fb_thinking,
                    tools=tools,
                    tool_executor=tool_executor,
                )
            except Exception as fallback_exc:
                logger.error(
                    "Fallback model also failed for agent '%s' (model: %s): %s",
                    agent_id, agent_config.fallback_model, fallback_exc,
                )
                raise RouterError(
                    agent_id, agent_config.fallback_model, fallback_exc
                ) from fallback_exc

    def _call(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        metadata: dict[str, str],
        agent_id: AgentId,
        thinking_param: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_executor: Any | None = None,
    ) -> RouterResult:
        """Single completion, or a read-only tool loop when tools are supplied."""
        kwargs = self._build_kwargs(model, messages, temperature, max_tokens, metadata, thinking_param, self._config.anthropic_api_key)

        if tools and tool_executor is not None:
            return self._run_tool_loop(kwargs, model, agent_id, tools, tool_executor)

        # Streaming lifts the connection-timeout ceiling on long responses. It is
        # skipped when tools are present (tool_call objects arrive fragmented across
        # chunks and need reassembly) and on the fallback path (reliability over
        # token ceiling — see complete(), which never sets streaming for fallback).
        agent_config = self._config.agents.get(agent_id)
        if not tools and getattr(agent_config, "streaming", False):
            return self._stream_completion(kwargs, model, agent_id)

        response = _with_backoff(
            lambda: litellm.completion(**kwargs), agent_id=agent_id, model=model,
        )
        return self._normalise(response, model, agent_id)

    def _stream_completion(
        self, kwargs: dict[str, Any], model: str, agent_id: AgentId
    ) -> RouterResult:
        """Accumulate a streamed completion into a RouterResult.

        LiteLLM (1.88.x) separates reasoning and output text on each chunk's delta:
        `delta.reasoning_content` carries thinking, `delta.content` carries output.
        Both fields coexist, so no block-type tracking is needed. Streaming chunks
        expose `.delta` (not `.message`), and usage/finish_reason only settle on the
        last chunk — so this builds RouterResult directly rather than via _normalise().
        """
        thinking_chunks: list[str] = []
        text_chunks: list[str] = []
        last_chunk: Any = None

        for attempt in range(_MAX_ATTEMPTS):
            thinking_chunks = []
            text_chunks = []
            last_chunk = None
            try:
                for chunk in litellm.completion(**kwargs, stream=True):
                    last_chunk = chunk
                    delta = chunk.choices[0].delta
                    rc = getattr(delta, "reasoning_content", None)
                    if rc:
                        thinking_chunks.append(rc)
                    c = getattr(delta, "content", None)
                    if c:
                        text_chunks.append(c)
                break  # stream completed successfully
            except _TRANSIENT_ERRORS as exc:
                if last_chunk is not None:
                    # Mid-stream failure: re-requesting from scratch would re-bill
                    # the full token budget (including expensive thinking tokens).
                    raise
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
                computed = _BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.2 * _BACKOFF_BASE)
                delay = max(computed, _retry_after(exc))
                logger.warning(
                    "Router(stream): agent=%s model=%s transient error before first chunk "
                    "(attempt %d/%d), retrying in %.2fs: %s",
                    agent_id, model, attempt + 1, _MAX_ATTEMPTS, delay, exc,
                )
                time.sleep(delay)

        if last_chunk is None:
            raise RouterError(
                agent_id, model,
                ValueError("streaming completion returned no chunks"),
            )

        thinking = "".join(thinking_chunks) or None
        text = "".join(text_chunks)
        json_text = self._extract_json(text)

        finish_reason = (
            getattr(last_chunk.choices[0], "finish_reason", None)
            if last_chunk is not None else None
        )
        truncated = finish_reason == "length"

        # On streaming, _hidden_params on the last chunk does not carry
        # litellm_call_id; _call_id falls back to the provider response id (chunk.id),
        # which is still a unique per-call identifier for cost attribution.
        litellm_call_id = self._call_id(last_chunk)
        usage = self._usage_streaming(last_chunk)

        logger.info(
            "Router(stream): agent=%s model=%s call_id=%s thinking=%s tokens=%s",
            agent_id, model, litellm_call_id, "yes" if thinking else "no", usage,
        )

        if truncated:
            logger.warning(
                "Router(stream): agent=%s finish_reason=length content=%d bytes tokens=%s"
                " — may be genuine truncation or transient API error; routing through gate",
                agent_id, len(json_text.encode()), usage,
            )

        return RouterResult(
            content=json_text,
            litellm_call_id=litellm_call_id,
            model_used=model,
            thinking=thinking,
            usage=usage,
            truncated=truncated,
        )

    def _run_tool_loop(
        self,
        base_kwargs: dict[str, Any],
        model: str,
        agent_id: AgentId,
        tools: list[dict[str, Any]],
        tool_executor: Any,
    ) -> RouterResult:
        """Drive call → tool_use → tool_result until a final JSON answer.

        The whole loop counts as ONE agent invocation for the global ceiling (the
        orchestrator increments once); per-inner-call cost is collected in
        result.tool_calls. A turn budget bounds the loop; on exhaustion we force a
        final no-tools call so the agent must produce its artifact.
        """
        messages: list[dict[str, Any]] = list(base_kwargs["messages"])
        collected: list[dict[str, Any]] = []
        max_turns = int(getattr(tool_executor, "max_tool_turns", 12))
        transient_exhausted = False

        for _ in range(max_turns):
            turn_kwargs = {**base_kwargs, "messages": messages, "tools": tools}
            try:
                response = _with_backoff(
                    lambda: litellm.completion(**turn_kwargs), agent_id=agent_id, model=model,
                )
            except _TRANSIENT_ERRORS:
                transient_exhausted = True
                logger.warning(
                    "Router: agent=%s model=%s retry budget exhausted mid-loop after %d attempts; "
                    "costs from failed retries not recorded in tool_calls",
                    agent_id, model, _MAX_ATTEMPTS,
                )
                break
            call_id = self._call_id(response)
            collected.append({"litellm_call_id": call_id, "usage": self._usage(response)})

            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None)
            if not tool_calls:
                result = self._normalise(response, model, agent_id)
                result.tool_calls = collected
                return result

            messages.append(self._assistant_tool_message(message))
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool_result = tool_executor.execute(name, args, call_id)
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": tool_result}
                )

        # Turn budget or retry budget exhausted — force a final answer with no tools offered.
        if transient_exhausted:
            logger.warning(
                "Router: agent=%s model=%s forcing final answer after transient-error exhaustion "
                "with %d accumulated messages",
                agent_id, model, len(messages),
            )
        else:
            logger.info("Tool loop budget exhausted for agent '%s'; forcing final answer.", agent_id)
        final_kwargs = {**base_kwargs, "messages": messages}
        try:
            response = _with_backoff(
                lambda: litellm.completion(**final_kwargs), agent_id=agent_id, model=model,
            )
        except _TRANSIENT_ERRORS as exc:
            raise RouterError(agent_id, model, exc) from exc
        result = self._normalise(response, model, agent_id)
        result.tool_calls = collected
        return result

    @staticmethod
    def _build_kwargs(
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        max_tokens: int,
        metadata: dict[str, str],
        thinking_param: dict[str, Any] | None,
        api_key: str = "",
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "metadata": metadata,
            "num_retries": 0,  # _with_backoff owns retry logic; prevents silent compounding
        }
        if api_key:
            kwargs["api_key"] = api_key
        if thinking_param is not None:
            kwargs["thinking"] = thinking_param
            # Anthropic requires temperature=1 for extended thinking; omit if model
            # doesn't accept the parameter at all.
            if _supports_temperature(model):
                kwargs["temperature"] = 1
        elif _supports_temperature(model):
            kwargs["temperature"] = temperature
        return kwargs

    @staticmethod
    def _assistant_tool_message(message: Any) -> dict[str, Any]:
        """Reconstruct the assistant turn (with tool_calls) for the next request."""
        return {
            "role": "assistant",
            "content": getattr(message, "content", "") or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in message.tool_calls
            ],
        }

    @staticmethod
    def _call_id(response: Any) -> str:
        return (
            getattr(response, "_hidden_params", {}).get("litellm_call_id")
            or getattr(response, "id", "")
            or ""
        )

    @staticmethod
    def _usage(response: Any) -> dict[str, Any]:
        if hasattr(response, "usage") and response.usage is not None:
            return dict(response.usage)
        return {}

    @staticmethod
    def _usage_streaming(last_chunk: Any) -> dict[str, Any]:
        """Usage for a streamed call. The last chunk's `.usage` is None; LiteLLM
        stashes the assembled Usage object in `_hidden_params["usage"]` (an object,
        not a dict)."""
        if last_chunk is None:
            return {}
        # A stream-final chunk may still expose usage directly in some versions.
        if getattr(last_chunk, "usage", None) is not None:
            return dict(last_chunk.usage)
        hidden = getattr(last_chunk, "_hidden_params", {}) or {}
        usage_obj = hidden.get("usage")
        if usage_obj is None:
            return {}
        try:
            return dict(usage_obj)
        except (TypeError, ValueError):
            return dict(getattr(usage_obj, "__dict__", {}))

    def _normalise(self, response: Any, model: str, agent_id: AgentId) -> RouterResult:
        """Extract JSON-bearing text + metadata from a completion response."""
        choice = response.choices[0]
        message = choice.message
        raw_content = getattr(message, "content", None)
        text, thinking = self._extract_content(raw_content, message)
        json_text = self._extract_json(text)

        truncated = getattr(choice, "finish_reason", None) == "length"

        litellm_call_id = self._call_id(response)
        usage = self._usage(response)

        logger.info(
            "Router: agent=%s model=%s call_id=%s thinking=%s tokens=%s",
            agent_id, model, litellm_call_id, "yes" if thinking else "no", usage,
        )

        if truncated:
            logger.warning(
                "Router: agent=%s finish_reason=length content=%d bytes tokens=%s"
                " — may be genuine truncation or transient API error; routing through gate",
                agent_id, len(json_text.encode()), usage,
            )

        return RouterResult(
            content=json_text,
            litellm_call_id=litellm_call_id,
            model_used=model,
            thinking=thinking,
            usage=usage,
            truncated=truncated,
        )

    @staticmethod
    def _extract_content(raw_content: Any, message: Any) -> tuple[str, str | None]:
        """Return (text, thinking) from a message whose content may be a string or a
        list of typed blocks (Anthropic extended-thinking shape via LiteLLM)."""
        # Some LiteLLM versions surface reasoning on a dedicated attribute.
        attr_thinking = getattr(message, "reasoning_content", None)

        if isinstance(raw_content, list):
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            for block in raw_content:
                if isinstance(block, dict):
                    btype = block.get("type")
                    if btype == "thinking":
                        thinking_parts.append(block.get("thinking", ""))
                    elif btype in ("text", None):
                        text_parts.append(block.get("text", ""))
                else:
                    btype = getattr(block, "type", None)
                    if btype == "thinking":
                        thinking_parts.append(getattr(block, "thinking", ""))
                    elif btype in ("text", None):
                        text_parts.append(getattr(block, "text", ""))
            thinking = "\n".join(p for p in thinking_parts if p) or attr_thinking
            return "".join(text_parts), thinking

        return (raw_content or ""), attr_thinking

    @staticmethod
    def _extract_json(text: str) -> str:
        """Return the JSON envelope object embedded in a model response.

        The response contract is: free-text reasoning first, then a single JSON
        object as the final content, with nothing after it. We honour that with a
        forward scan backed by the real JSON lexer rather than hand-rolled brace
        counting:

          1. Strip an optional ``` / ```json markdown fence.
          2. From each `{` in turn, ask json.JSONDecoder.raw_decode to parse exactly
             one value. raw_decode uses the real lexer — correct string, escape, and
             brace handling — and ignores any text trailing the value it parses. So
             leading prose, braces *inside* that prose, embedded code containing
             `{ } " \\"`, and any trailing snippet or postscript all fall away for
             free, with none of the desync failure modes of a hand-rolled scanner.
          3. Prefer the last parsed object carrying the envelope keys (disambiguates
             a JSON snippet quoted in the reasoning from the real envelope, and a
             re-emitted draft from its final version); else fall back to the last
             object that parses at all, since the contract puts the output last.

        Returns the stripped text unchanged when nothing parses, so the schema_valid
        gate rejects it as malformed with a useful error.
        """
        s = text.strip()
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
            s = re.sub(r"\n?```$", "", s).strip()

        decoder = json.JSONDecoder()
        envelope_match: str | None = None
        last_object: str | None = None
        start = s.find("{")
        while start != -1:
            try:
                obj, end = decoder.raw_decode(s, start)
            except json.JSONDecodeError:
                # Not the start of a valid object (a brace in prose, a regex, an
                # unterminated fragment) — try the next `{`.
                start = s.find("{", start + 1)
                continue
            if isinstance(obj, dict):
                segment = s[start:end]
                last_object = segment
                if _ENVELOPE_KEYS.issubset(obj.keys()):
                    envelope_match = segment
            # Resume past the object we just consumed, so only top-level objects
            # are considered (never the nested objects inside one we already took).
            start = s.find("{", end)

        # Prefer the last well-formed envelope; else the last top-level object (the
        # contract puts the real output last); else the text as-is for the gate to reject.
        if envelope_match is not None:
            return envelope_match
        return last_object if last_object is not None else s
