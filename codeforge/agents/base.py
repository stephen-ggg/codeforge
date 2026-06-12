"""
agents/base.py — Shared agent base class.

Every LLM agent inherits from BaseAgent. The base class handles:
  1. Loading the system prompt from the config-specified path
  2. Assembling the user turn from the context package + optional reprompt
  3. Calling the model router
  4. Returning the raw response string

Validation of the response is the orchestrator's job — agents never validate
their own output. The raw string is returned as-is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codeforge.config.config_loader import ConfigSnapshot
from codeforge.firewall.assembler import ContextPackage
from codeforge.model_router.router import ModelRouter
from codeforge.schemas.contracts import AgentId, RePromptContext


class BaseAgent:
    """
    Shared invocation logic for all LLM agents.

    Subclasses override build_user_turn() to format their specific input.
    The system prompt is loaded once on first invoke and cached.
    """

    def __init__(
        self,
        agent_id: AgentId,
        router: ModelRouter,
        config: ConfigSnapshot,
    ) -> None:
        self._agent_id = agent_id
        self._router = router
        self._config = config
        self._system_prompt: str | None = None

    @property
    def agent_id(self) -> AgentId:
        return self._agent_id

    def invoke(
        self,
        context_package: ContextPackage,
        reprompt: RePromptContext | None = None,
    ) -> str:
        """
        Invoke the agent and return the raw LLM response string.

        Steps:
          1. Load system prompt from config (cached after first load)
          2. Build user turn from context_package + reprompt
          3. Call router.complete()
          4. Return raw response string — validation happens in the orchestrator
        """
        system_prompt = self._load_system_prompt()
        user_turn = self.build_user_turn(context_package, reprompt)
        result = self._router.complete(
            agent_id=self._agent_id,
            system_prompt=system_prompt,
            user_turn=user_turn,
            run_id=context_package.run_id,
        )
        return result.content

    def build_user_turn(
        self,
        context_package: ContextPackage,
        reprompt: RePromptContext | None = None,
    ) -> str:
        """
        Format the user turn from the context package.

        Subclasses override this to provide agent-specific formatting.
        The default implementation serialises the full context package as JSON,
        which is adequate for testing but subclasses provide structured formatting.
        """
        import json
        parts: dict[str, Any] = {
            "artifacts": {k: v.model_dump() for k, v in context_package.artifacts.items()},
            "state_documents": context_package.state_documents,
        }
        if reprompt is not None:
            parts["reprompt"] = reprompt.model_dump()
        return json.dumps(parts, ensure_ascii=False)

    def _load_system_prompt(self) -> str:
        """
        Load the system prompt from the path specified in the agent's config block.
        Cached after first load.
        """
        if self._system_prompt is not None:
            return self._system_prompt

        agent_config = self._config.agents.get(self._agent_id)
        if agent_config is None:
            raise ValueError(f"No config found for agent '{self._agent_id}'")

        prompt_path_str = agent_config.system_prompt
        if not prompt_path_str:
            raise ValueError(
                f"Agent '{self._agent_id}' has no system_prompt path configured"
            )

        # Resolve relative to the package root (where pipeline.config.yaml lives)
        package_root = Path(__file__).parent.parent / "config"
        prompt_path = package_root / prompt_path_str

        if not prompt_path.exists():
            raise FileNotFoundError(
                f"System prompt not found for agent '{self._agent_id}': {prompt_path}"
            )

        self._system_prompt = prompt_path.read_text(encoding="utf-8")
        return self._system_prompt
