"""
agents/base.py — Shared agent base class.

Every LLM agent inherits from BaseAgent. The base class handles:
  1. Loading the system prompt from the config-specified path
  2. Assembling the user turn from the context package + optional reprompt,
     wrapping each input in an XML tag so the model can tell instructions from data
  3. Calling the model router
  4. Returning the raw response string

Validation of the response is the orchestrator's job — agents never validate their
own output.

=== CHANGES FROM PRE-XML VERSION ===
  * build_user_turn() now emits XML-delimited sections (one tag per artifact / state
    document / injected context) instead of one flat JSON blob. Tag names come from the
    prompt manifest so they always match what the rendered prompt tells the agent to expect.
  * __init__ accepts an optional `tag_overrides` map (manifest input_tags) — defaults to
    identity (tag == artifact_type / document name), which is the common case.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codeforge.config.config_loader import ConfigSnapshot
from codeforge.firewall.assembler import ContextPackage
from codeforge.model_router.router import ModelRouter
from codeforge.schemas.contracts import AgentId, RePromptContext


class BaseAgent:
    """Shared invocation logic for all LLM agents."""

    def __init__(
        self,
        agent_id: AgentId,
        router: ModelRouter,
        config: ConfigSnapshot,
        tag_overrides: dict[str, str] | None = None,
    ) -> None:
        self._agent_id = agent_id
        self._router = router
        self._config = config
        self._system_prompt: str | None = None
        # Maps a canonical input name (artifact_type / state-doc name / "reprompt") to the
        # XML tag to wrap it in. Identity by default; the orchestrator passes the manifest's
        # input_tags slice when a rename is in effect.
        self._tag_overrides = tag_overrides or {}

    @property
    def agent_id(self) -> AgentId:
        return self._agent_id

    def invoke(
        self,
        context_package: ContextPackage,
        reprompt: RePromptContext | None = None,
    ) -> str:
        """Invoke the agent and return the raw LLM response string (JSON-bearing text)."""
        system_prompt = self._load_system_prompt()
        user_turn = self.build_user_turn(context_package, reprompt)
        result = self._router.complete(
            agent_id=self._agent_id,
            system_prompt=system_prompt,
            user_turn=user_turn,
            run_id=context_package.run_id,
        )
        return result.content

    # ------------------------------------------------------------------
    # User-turn assembly — XML-delimited sections
    # ------------------------------------------------------------------

    def _tag_for(self, name: str) -> str:
        return self._tag_overrides.get(name, name)

    def _section(self, tag: str, body: str) -> str:
        return f"<{tag}>\n{body}\n</{tag}>"

    def build_user_turn(
        self,
        context_package: ContextPackage,
        reprompt: RePromptContext | None = None,
    ) -> str:
        """Format the user turn as a sequence of XML-delimited sections.

        Each artifact and each state document the firewall allowed becomes its own
        section, tagged by its canonical name (or a manifest override). Artifacts are
        serialised as pretty JSON inside the tag; state documents are already markdown.
        A reprompt, when present, is the final section.

        Subclasses may override to thread in agent-specific injected contexts
        (retry_context, code_fix_context, spec_gap_context, run_mode, existing_interfaces).
        The default body covers the artifact + state-doc + reprompt sections common to all.
        """
        sections: list[str] = []

        # Artifacts (firewall-gated) — JSON inside the tag.
        for artifact_type, output in context_package.artifacts.items():
            tag = self._tag_for(artifact_type)
            body = json.dumps(output.model_dump(), ensure_ascii=False, indent=2)
            sections.append(self._section(tag, body))

        # State documents — already rendered markdown.
        for document, md in context_package.state_documents.items():
            tag = self._tag_for(document)
            sections.append(self._section(tag, md))

        # Reprompt — always last so the correction instruction is most salient.
        if reprompt is not None:
            body = json.dumps(reprompt.model_dump(), ensure_ascii=False, indent=2)
            sections.append(self._section(self._tag_for("reprompt"), body))

        return "\n\n".join(sections)

    # ------------------------------------------------------------------
    # System prompt loading (unchanged)
    # ------------------------------------------------------------------

    def _load_system_prompt(self) -> str:
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

        package_root = Path(__file__).parent.parent / "config"
        prompt_path = package_root / prompt_path_str

        if not prompt_path.exists():
            raise FileNotFoundError(
                f"System prompt not found for agent '{self._agent_id}': {prompt_path}"
            )

        self._system_prompt = prompt_path.read_text(encoding="utf-8")
        return self._system_prompt
