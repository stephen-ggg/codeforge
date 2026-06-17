"""
orchestrator/event_log.py — Append-only codeforge orchestrator event log.

Writes one JSON object per line to events.jsonl.
Sequence numbers are monotonically increasing and are the authoritative ordering.
Timestamps are wall-clock and are for external correlation only.

Also implements EventLogProtocol from firewall/audit.py so the assembler
can emit AccessEvents through the same log.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from codeforge.schemas.contracts import (
    AccessEvent,
    CodeforgeRun,
    CountersSnapshot,
    GateEvent,
    GateRule,
    HandoffEvent,
    HandoffInvocationType,
    HumanInteractionEvent,
    HumanInteractionKind,
    LogActor,
    OrchestratorEventUnion,
    RePromptContext,
    RoutingDecision,
    RoutingEvent,
    StateWriteEvent,
    StateWriteTarget,
    ToolCallEvent,
    WriteSource,
)
from codeforge.firewall.audit import EventLogProtocol


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    import uuid
    return str(uuid.uuid4())


class EventLog(EventLogProtocol):
    """
    Append-only event log for a single codeforge run.

    Thread-safe: a lock protects the sequence counter and file writes.
    All five event types are emitted through typed factory methods.
    """

    def __init__(self, run_dir: Path, run_id: str, codeforge_version: str) -> None:
        self._run_dir = run_dir
        self._run_id = run_id
        self._codeforge_version = codeforge_version
        self._events_path = run_dir / "events.jsonl"
        self._run_path = run_dir / "codeforge_run.json"
        self._sequence = 0
        self._lock = threading.Lock()
        run_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Core emit
    # ------------------------------------------------------------------

    def emit(self, event: OrchestratorEventUnion) -> None:
        """
        Assign sequence number and append event to events.jsonl.
        Sequence is assigned under the lock — monotonically increasing.
        """
        with self._lock:
            self._sequence += 1
            # Mutate the sequence field — events are constructed with sequence=0
            # then stamped here (avoids threading races in callers)
            event_dict = event.model_dump()
            event_dict["sequence"] = self._sequence
            line = json.dumps(event_dict, ensure_ascii=False)
            with self._events_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def get_sequence(self) -> int:
        """Current sequence counter value."""
        with self._lock:
            return self._sequence

    def update_run_snapshot(self, run: CodeforgeRun) -> None:
        """
        Overwrite codeforge_run.json with the current CodeforgeRun snapshot.
        Called by the state machine on every state transition.
        """
        with self._lock:
            self._run_path.write_text(
                json.dumps(run.model_dump(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    # ------------------------------------------------------------------
    # EventLogProtocol implementation (for firewall/audit.py)
    # ------------------------------------------------------------------

    def emit_access_event(self, event: AccessEvent) -> None:
        """Write an AccessEvent to the log as a plain JSON line (not a typed event)."""
        with self._lock:
            self._sequence += 1
            record = {"event_type": "access", "sequence": self._sequence, **event.model_dump()}
            line = json.dumps(record, ensure_ascii=False)
            with self._events_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    # ------------------------------------------------------------------
    # Typed event factory methods
    # ------------------------------------------------------------------

    def _base_fields(self, counters: CountersSnapshot) -> dict[str, Any]:
        return {
            "event_id": _uuid(),
            "run_id": self._run_id,
            "sequence": 0,          # stamped in emit()
            "timestamp": _now(),
            "codeforge_version": self._codeforge_version,
            "counters": counters.model_dump(),
        }

    def emit_handoff(
        self,
        to_agent: LogActor,
        invocation_type: HandoffInvocationType,
        counters: CountersSnapshot,
        assembly_id: str | None = None,
        context_package_ref: str | None = None,
        stripped_fields: list[str] | None = None,
        reprompt_reason: str | None = None,
        litellm_call_id: str | None = None,
    ) -> None:
        event = HandoffEvent(
            **self._base_fields(counters),
            event_type="handoff",
            to_agent=to_agent,
            invocation_type=invocation_type,
            assembly_id=assembly_id,
            context_package_ref=context_package_ref,
            stripped_fields=stripped_fields or [],
            reprompt_reason=reprompt_reason,  # type: ignore[arg-type]
            litellm_call_id=litellm_call_id,
        )
        self.emit(event)

    def emit_gate(
        self,
        rule: GateRule,
        passed: bool,
        source_agent: LogActor,
        counters: CountersSnapshot,
        detail: str,
        artifact_ref: str | None = None,
    ) -> None:
        event = GateEvent(
            **self._base_fields(counters),
            event_type="gate",
            rule=rule,
            passed=passed,
            source_agent=source_agent,
            artifact_ref=artifact_ref,
            detail=detail,
        )
        self.emit(event)

    def emit_routing(
        self,
        routing_table_row: str,
        decision: RoutingDecision,
        next_state: str,
        counters: CountersSnapshot,
        counter_deltas: dict[str, int] | None = None,
        counter_resets: list[str] | None = None,
    ) -> None:
        event = RoutingEvent(
            **self._base_fields(counters),
            event_type="routing",
            routing_table_row=routing_table_row,
            decision=decision,
            counter_deltas=counter_deltas or {},
            counter_resets=counter_resets or [],
            next_state=next_state,
        )
        self.emit(event)

    def emit_state_write(
        self,
        document: StateWriteTarget,
        write_source: WriteSource,
        gate_condition: str,
        content_hash_before: str,
        content_hash_after: str,
        counters: CountersSnapshot,
    ) -> None:
        event = StateWriteEvent(
            **self._base_fields(counters),
            event_type="state_write",
            document=document,
            write_source=write_source,
            gate_condition=gate_condition,
            content_hash_before=content_hash_before,
            content_hash_after=content_hash_after,
        )
        self.emit(event)

    def emit_tool_call(
        self,
        agent_id: LogActor,
        tool_name: str,
        tool_input: dict[str, Any],
        decision: str,
        result_summary: str,
        latency_ms: float,
        counters: CountersSnapshot,
        deny_reason: str | None = None,
        litellm_call_id: str | None = None,
    ) -> None:
        event = ToolCallEvent(
            **self._base_fields(counters),
            event_type="tool_call",
            agent_id=agent_id,
            tool_name=tool_name,
            tool_input=tool_input,
            decision=decision,  # type: ignore[arg-type]
            deny_reason=deny_reason,
            result_summary=result_summary,
            latency_ms=latency_ms,
            litellm_call_id=litellm_call_id,
        )
        self.emit(event)

    def emit_human_interaction(
        self,
        interaction_kind: HumanInteractionKind,
        direction: str,
        interaction_id: str,
        payload_ref: str,
        counters: CountersSnapshot,
        latency_seconds: float | None = None,
    ) -> None:
        event = HumanInteractionEvent(
            **self._base_fields(counters),
            event_type="human_interaction",
            interaction_kind=interaction_kind,
            direction=direction,  # type: ignore[arg-type]
            interaction_id=interaction_id,
            payload_ref=payload_ref,
            latency_seconds=latency_seconds,
        )
        self.emit(event)
