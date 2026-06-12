"""
model_router/router.py — LiteLLM-based model router.

THIS IS THE ONLY FILE IN THE PIPELINE THAT IMPORTS LITELLM.
Swapping the LLM provider library means editing this file only.

Responsibilities:
  - Translate a generic complete() call into the correct LiteLLM API call
  - Look up model, temperature, max_tokens, fallback_model, thinking from config
  - Enable Anthropic extended thinking when configured (claude-* models only);
    fall back to a <thinking> scratchpad instruction for non-Anthropic models
  - Normalise the response: strip any thinking block, extract the JSON text so the
    orchestrator always receives a clean JSON string regardless of provider
  - Stamp every call with agent_id, run_id, and pipeline metadata
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

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import litellm  # noqa: PGH003

from codeforge.config.config_loader import ConfigSnapshot
from codeforge.schemas.contracts import AgentId

logger = logging.getLogger(__name__)

litellm.suppress_debug_info = True

# Appended to the user turn ONLY when thinking is configured but the model can't do it
# natively (non-Anthropic provider). Anthropic models get a real thinking block instead.
_SCRATCHPAD_INSTRUCTION = (
    "\n\n---\n\n"
    "Before your output object, reason through the problem in plain text. "
    "When you are done reasoning, output the JSON object as the final thing in your "
    "response. The JSON object must be the last content you produce."
)


@dataclass
class RouterResult:
    """Result of a single LiteLLM completion call."""
    content: str                            # extracted JSON-bearing text (thinking stripped)
    litellm_call_id: str                    # authoritative cost-attribution identifier
    model_used: str                         # actual model that responded (may be fallback)
    thinking: str | None = None             # raw reasoning, for raw_outputs/ — not validated
    usage: dict[str, Any] = field(default_factory=dict)


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
    ) -> RouterResult:
        """Call the configured LLM for agent_id and return a normalised result.

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
            "pipeline": self._config.pipeline,
            "pipeline_version": self._config.pipeline,
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
    ) -> RouterResult:
        """Make a single litellm.completion() call and normalise the result."""
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "metadata": metadata,
        }
        if thinking_param is not None:
            # Anthropic requires temperature == 1 when extended thinking is enabled.
            kwargs["thinking"] = thinking_param
            kwargs["temperature"] = 1
        else:
            kwargs["temperature"] = temperature

        response = litellm.completion(**kwargs)

        message = response.choices[0].message
        raw_content = getattr(message, "content", None)
        text, thinking = self._extract_content(raw_content, message)
        json_text = self._extract_json(text)

        litellm_call_id: str = (
            getattr(response, "_hidden_params", {}).get("litellm_call_id")
            or getattr(response, "id", "")
            or ""
        )

        usage: dict[str, Any] = {}
        if hasattr(response, "usage") and response.usage is not None:
            usage = dict(response.usage)

        logger.info(
            "Router: agent=%s model=%s call_id=%s thinking=%s tokens=%s",
            agent_id, model, litellm_call_id, "yes" if thinking else "no", usage,
        )

        return RouterResult(
            content=json_text,
            litellm_call_id=litellm_call_id,
            model_used=model,
            thinking=thinking,
            usage=usage,
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
        """Strip markdown fences / leading prose and return the final top-level JSON
        object substring. With native thinking this is a no-op (text is already pure
        JSON); it exists for the scratchpad fallback path and stray-fence robustness."""
        s = text.strip()
        if s.startswith("```"):
            s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
            s = re.sub(r"\n?```$", "", s).strip()
        if s.startswith("{") and s.endswith("}"):
            return s
        depth, start, last = 0, None, None
        for i, ch in enumerate(s):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    last = s[start:i + 1]
        return last if last is not None else s
