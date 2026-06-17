"""
tools/executor.py — Tool dispatch + the two enforcement boundaries.

The executor is the single place where a tool call is (a) checked against the
per-agent allowlist (defense-in-depth — a forbidden agent should never be handed
tools in the first place, but if it reaches here it is denied and logged), (b)
path-jailed, then (c) executed. Every call — allowed or denied — produces a
ToolCallEvent in the run event stream AND an AccessEvent appended both to the
agent's context package and to the firewall access stream.

TOOL_SCHEMAS are OpenAI-style function definitions, which LiteLLM forwards to
the provider; tool calls come back normalised on `message.tool_calls`.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from codeforge.firewall.manifest import FirewallManifest
from codeforge.schemas.contracts import AccessEvent, CountersSnapshot, LogActor
from codeforge.tools.jail import JailError
from codeforge.tools.readonly import (
    ToolOutput,
    find_references,
    list_dir,
    read_file,
    search_code,
)

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": (
                "Regex-search the existing source repository. Returns matching "
                "lines as 'path:line: text'. Use this to locate functions, "
                "call sites, and patterns before editing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "A Python regular expression."},
                    "glob": {"type": "string", "description": "Optional filename glob filter, e.g. '*.py'."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the repository, optionally a 1-based line range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the repository root."},
                    "start": {"type": "integer", "description": "1-based start line (optional)."},
                    "end": {"type": "integer", "description": "Inclusive end line (optional)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_references",
            "description": "Find references to a symbol (word-boundary search) across the repository.",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string", "description": "The identifier to look for."}},
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List entries of a repository directory (directories end with '/').",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory path relative to repo root; defaults to root."}},
                "required": [],
            },
        },
    },
]


class _EventSink(Protocol):
    """Minimal event-log surface the executor needs (eases testing)."""

    def emit_tool_call(
        self,
        agent_id: LogActor,
        tool_name: str,
        tool_input: dict[str, Any],
        decision: str,
        result_summary: str,
        latency_ms: float,
        counters: CountersSnapshot,
        deny_reason: str | None = ...,
        litellm_call_id: str | None = ...,
    ) -> None: ...

    def emit_access_event(self, event: AccessEvent) -> None: ...


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ToolExecutor:
    """Per-agent-invocation tool dispatcher. One instance per agent invocation."""

    def __init__(
        self,
        *,
        root: Path | str,
        agent_id: LogActor,
        manifest: FirewallManifest,
        event_log: _EventSink,
        counters: CountersSnapshot,
        assembly_id: str = "",
        max_tool_turns: int = 12,
    ) -> None:
        self._root = Path(root)
        self._agent_id = agent_id
        self._enabled = manifest.tools_enabled_for(agent_id)
        self._event_log = event_log
        self._counters = counters
        self._assembly_id = assembly_id
        self.max_tool_turns = max_tool_turns
        # AccessEvents accumulated this invocation — the orchestrator appends
        # them to the context package so the audit surface stays complete.
        self.access_events: list[AccessEvent] = []
        self.call_count = 0

    def tool_schemas(self) -> list[dict[str, Any]] | None:
        """Return the tool schemas if this agent is allowed tools, else None."""
        return TOOL_SCHEMAS if self._enabled else None

    def execute(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        litellm_call_id: str | None = None,
    ) -> str:
        """Run one tool call. Always logs; never raises (errors become content)."""
        self.call_count += 1
        start = time.monotonic()
        decision = "allow"
        deny_reason: str | None = None
        summary = ""
        content = ""

        if not self._enabled:
            # Defense in depth — a forbidden agent must never reach a tool.
            decision = "deny"
            deny_reason = "agent not in tool_access.enabled_agents"
            content = f"DENIED: {deny_reason}"
            summary = "denied"
        else:
            try:
                output = self._dispatch(tool_name, tool_input)
                content = output.content
                summary = output.summary
            except JailError as exc:
                decision = "deny"
                deny_reason = str(exc)
                content = f"DENIED: {exc}"
                summary = "denied"
            except Exception as exc:  # noqa: BLE001 — surface, never crash the loop
                decision = "deny"
                deny_reason = f"tool error: {exc}"
                content = f"ERROR: {exc}"
                summary = "error"

        latency_ms = (time.monotonic() - start) * 1000.0

        self._event_log.emit_tool_call(
            agent_id=self._agent_id,
            tool_name=tool_name,
            tool_input=tool_input,
            decision=decision,
            result_summary=summary,
            latency_ms=latency_ms,
            counters=self._counters,
            deny_reason=deny_reason,
            litellm_call_id=litellm_call_id,
        )

        event = AccessEvent(
            artifact_id=f"tool:{tool_name}:{self._arg_repr(tool_input)}",
            requesting_agent=self._agent_id,
            decision=decision,  # type: ignore[arg-type]
            reason_code=deny_reason or "tool_call",
            assembly_id=self._assembly_id,
            timestamp=_now(),
        )
        self.access_events.append(event)
        self._event_log.emit_access_event(event)

        return content

    def _dispatch(self, tool_name: str, tool_input: dict[str, Any]) -> ToolOutput:
        if tool_name == "search_code":
            return search_code(self._root, tool_input["query"], tool_input.get("glob"))
        if tool_name == "read_file":
            return read_file(
                self._root, tool_input["path"], tool_input.get("start"), tool_input.get("end")
            )
        if tool_name == "find_references":
            return find_references(self._root, tool_input["symbol"])
        if tool_name == "list_dir":
            return list_dir(self._root, tool_input.get("path", "."))
        raise ValueError(f"unknown tool: {tool_name}")

    @staticmethod
    def _arg_repr(tool_input: dict[str, Any]) -> str:
        return ", ".join(f"{k}={str(v)[:60]}" for k, v in tool_input.items())
