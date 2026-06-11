"""
model_router/router.py — LiteLLM-based model router.

THIS IS THE ONLY FILE IN THE PIPELINE THAT IMPORTS LITELLM.
Swapping the LLM provider library means editing this file only.

Responsibilities:
  - Translate a generic complete() call into the correct LiteLLM API call
  - Look up model, temperature, max_tokens, fallback_model from config
  - Stamp every call with agent_id, run_id, and pipeline metadata
  - Capture litellm_call_id as the authoritative cost-attribution identifier
  - On provider error: retry once with fallback_model if configured
  - Return RouterResult — raw text content, call id, model used, token usage

The router does NOT validate response content. That is the orchestrator's job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import litellm  # noqa: PGH003

from codeforge.config.config_loader import ConfigSnapshot
from codeforge.schemas.contracts import AgentId

logger = logging.getLogger(__name__)

# Silence LiteLLM's verbose success logging — we log at the router level instead
litellm.suppress_debug_info = True


@dataclass
class RouterResult:
    """Result of a single LiteLLM completion call."""
    content: str                            # raw text response from the model
    litellm_call_id: str                    # authoritative cost-attribution identifier
    model_used: str                         # actual model that responded (may be fallback)
    usage: dict[str, Any] = field(default_factory=dict)  # token counts from provider


class RouterError(Exception):
    """Raised when the router cannot get a response after primary + fallback attempts."""

    def __init__(self, agent_id: AgentId, model: str, cause: Exception) -> None:
        self.agent_id = agent_id
        self.model = model
        self.cause = cause
        super().__init__(
            f"Router failed for agent '{agent_id}' on model '{model}': {cause}"
        )


class ModelRouter:
    """
    Routes agent completion requests through LiteLLM.

    One instance per pipeline run. Thread-safe for reading config;
    each complete() call is independent.
    """

    def __init__(self, config: ConfigSnapshot) -> None:
        self._config = config

    def complete(
        self,
        agent_id: AgentId,
        system_prompt: str,
        user_turn: str,
        run_id: str,
    ) -> RouterResult:
        """
        Call the configured LLM for agent_id and return the result.

        Steps:
          1. Look up model, temperature, max_tokens, fallback_model from config
          2. Build metadata dict for cost attribution and audit
          3. Call litellm.completion() with the primary model
          4. On failure: retry once with fallback_model if configured
          5. Return RouterResult

        Raises:
            RouterError: if both primary and fallback calls fail.
        """
        agent_config = self._config.agents.get(agent_id)
        if agent_config is None:
            raise RouterError(
                agent_id,
                "<unknown>",
                ValueError(f"No config found for agent '{agent_id}'"),
            )

        metadata = {
            "agent_id": agent_id,
            "run_id": run_id,
            "pipeline": self._config.pipeline,
            "pipeline_version": self._config.pipeline,
        }

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_turn},
        ]

        # Primary attempt
        try:
            return self._call(
                model=agent_config.model,
                messages=messages,
                temperature=agent_config.temperature,
                max_tokens=agent_config.max_tokens,
                metadata=metadata,
                agent_id=agent_id,
            )
        except Exception as primary_exc:
            logger.warning(
                "Primary model failed for agent '%s' (model: %s): %s",
                agent_id,
                agent_config.model,
                primary_exc,
            )

            if not agent_config.fallback_model:
                raise RouterError(agent_id, agent_config.model, primary_exc) from primary_exc

            # Fallback attempt — one retry only
            logger.info(
                "Retrying agent '%s' with fallback model '%s'",
                agent_id,
                agent_config.fallback_model,
            )
            try:
                return self._call(
                    model=agent_config.fallback_model,
                    messages=messages,
                    temperature=agent_config.temperature,
                    max_tokens=agent_config.max_tokens,
                    metadata=metadata,
                    agent_id=agent_id,
                )
            except Exception as fallback_exc:
                logger.error(
                    "Fallback model also failed for agent '%s' (model: %s): %s",
                    agent_id,
                    agent_config.fallback_model,
                    fallback_exc,
                )
                raise RouterError(agent_id, agent_config.fallback_model, fallback_exc) from fallback_exc

    def _call(
        self,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        metadata: dict[str, str],
        agent_id: AgentId,
    ) -> RouterResult:
        """
        Make a single litellm.completion() call and extract the result.

        litellm_call_id is captured from the response object.
        Provider metadata passthrough is best-effort and varies by provider.
        """
        response = litellm.completion(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            metadata=metadata,
        )

        # Extract content — LiteLLM returns an OpenAI-compatible response object
        content: str = response.choices[0].message.content or ""

        # litellm_call_id: prefer the LiteLLM-level id; fall back to the provider id
        litellm_call_id: str = (
            getattr(response, "_hidden_params", {}).get("litellm_call_id")
            or getattr(response, "id", "")
            or ""
        )

        # Token usage — best-effort, varies by provider
        usage: dict[str, Any] = {}
        if hasattr(response, "usage") and response.usage is not None:
            usage = dict(response.usage)

        logger.info(
            "Router: agent=%s model=%s call_id=%s tokens=%s",
            agent_id,
            model,
            litellm_call_id,
            usage,
        )

        return RouterResult(
            content=content,
            litellm_call_id=litellm_call_id,
            model_used=model,
            usage=usage,
        )
