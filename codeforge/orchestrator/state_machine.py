"""
orchestrator/state_machine.py — Codeforge orchestrator state machine.

Owns the CodeforgeRun object. Drives codeforge through all phases.
Calls the assembler, model router, validator, routing table, and event log
in the correct sequence for each phase.

Key invariants:
  - agent_call_count checked BEFORE every LLM invocation (pre-invocation gate)
  - pending_writes never written to disk inside this module
  - Every invocation preceded by handoff event, followed by gate + routing events
  - block flags halt immediately — no retry
  - All re-prompts constructed here, never by agents
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn, cast

logger = logging.getLogger(__name__)

from codeforge.config.config_loader import ConfigSnapshot
from codeforge.firewall.assembler import ContextAssembler, ContextPackage
from codeforge.firewall.manifest import FirewallManifest, load_manifest
from codeforge.model_router.router import ModelRouter
from codeforge.tools.executor import ToolExecutor
from codeforge.orchestrator.event_log import EventLog
from codeforge.orchestrator.gates import GateEvaluator, GateResult
from codeforge.orchestrator.pending_writes import PendingWrites
from codeforge.orchestrator.routing import (
    RoutingOutcome,
    apply_outcome,
    route_malformed,
    route_truncated,
    route_block_flag,
    route_ceiling_exceeded,
    route_low_confidence,
    route_low_confidence_reprompt,
    route_requirements_clarify,
    route_requirements_complete,
    route_requirements_confirmed,
    route_requirements_rejected,
    route_architecture_valid,
    route_architecture_invalid,
    route_architecture_lowconf,
    route_coding_no_requirements_txt,
    route_coding_ac_gap,
    route_coding_valid,
    route_code_review_fail,
    route_code_review_pass,
    route_security_review_fail,
    route_security_review_pass,
    route_test_design_covmap_invalid,
    route_test_design_valid,
    route_test_execution_error,
    route_test_analysis_pass,
    route_test_analysis_code_bug,
    route_test_analysis_test_bug,
    route_test_analysis_spec_gap,
    route_test_analysis_ambiguous,
    route_test_analysis_error,
    route_test_analysis_recoverable_error,
)
from codeforge.orchestrator.state_writer import flush_pending_writes
from codeforge.schemas.contracts import (
    AgentId,
    AgentOutput,
    ArchitectureDesignerOutput,
    ModuleInterfaces,
    ArtifactRef,
    ArtifactType,
    CodeArtifact,
    CodeReviewerOutput,
    CoderOutput,
    CodeforgeRun,
    CodeforgeStatus,
    CountersSnapshot,
    EscalationEvent,
    EscalationReason,
    HandoffInvocationType,
    LogActor,
    LowConfidenceRePrompt,
    ReentryState,
    RePromptContext,
    RequirementsDoc,
    RetryCounters,
    ReviewReport,
    SecurityReport,
    SecurityReviewerOutput,
    TestAnalysis,
    TestAnalystOutput,
    ArchitectureDoc,
    TestDesignerOutput,
    TestSuite,
)
from codeforge.schemas.validation import OutputValidator
from codeforge.store.artifact_store import ArtifactStore
from codeforge.store.project_state import ProjectStateStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


# Which stack-profile prompt fragment each agent receives. Reviewers share one fragment;
# requirements_analyst and test_analyst are stack-agnostic and receive none.
_STACK_FRAGMENT_FOR_AGENT: dict[str, str] = {
    "architecture_designer": "architecture",
    "coder": "coder",
    "code_reviewer": "reviewer",
    "security_reviewer": "reviewer",
    "test_designer": "test_designer",
}


class EscalationError(Exception):
    """Raised when codeforge must escalate to a human."""

    def __init__(self, reason: EscalationReason, context: str = "", phase: str | None = None) -> None:
        self.reason = reason
        self.context = context
        self.phase = phase
        super().__init__(f"Codeforge escalated: {reason} — {context}")


class StateMachine:
    """
    Codeforge orchestrator state machine.

    One instance per codeforge run. Not thread-safe — runs sequentially.
    """

    def __init__(
        self,
        config: ConfigSnapshot,
        project_dir: Path,
        run_log_dir: Path,
    ) -> None:
        self._config = config
        self._project_dir = project_dir
        self._run_log_dir = run_log_dir

        # Stores. The artifact store must be rooted at the per-run directory
        # (run-logs/<run_id>/), which is only known once the run_id exists, so it is
        # created in start_run/resume_run. It stays None until then: a base-rooted
        # placeholder would create stray run-logs/{artifacts,raw_outputs,failed_artifacts}/
        # dirs and, worse, silently absorb writes to the base if re-rooting were ever
        # skipped. Reach it via the artifact_store property so any unrooted use fails loudly.
        self._project_state = ProjectStateStore(project_dir)
        self._artifact_store: ArtifactStore | None = None

        # Core components (initialised in start_run)
        self._run: CodeforgeRun | None = None
        self._pending: PendingWrites | None = None
        self._event_log: EventLog | None = None
        self._validator: OutputValidator | None = None
        self._gates: GateEvaluator | None = None
        self._router: ModelRouter | None = None
        self._assembler: ContextAssembler | None = None
        self._manifest: "FirewallManifest | None" = None

        # Tracks which phase is currently executing so escalation events can carry
        # a suggested reentry state for the human operator.
        self._current_phase: "ReentryState | None" = None

        # Whether the most recent _invoke_agent response was cut off at max_tokens
        # (finish_reason=length). Consumed by _handle_structural_failure to route a
        # truncated response through the bounded truncation path rather than treating
        # it as a generic malformed_output failure. Set fresh on every invocation.
        self._last_truncated: bool = False

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def start_run(
        self,
        run_mode: str,
        human_brief: str,
    ) -> CodeforgeRun:
        """
        Initialise a new CodeforgeRun and all components.
        Returns the run object — caller can inspect status after execute().
        """
        run_id = _new_run_id()
        run_log_dir = self._run_log_dir / run_id
        run_log_dir.mkdir(parents=True, exist_ok=True)
        # Root all artifact I/O (validated / failed / raw) under this run's directory.
        self._artifact_store = ArtifactStore(run_log_dir)

        from typing import Literal as Lit
        run_mode_typed = cast(Lit["new_project", "continuation"], run_mode)
        self._run = CodeforgeRun(
            run_id=run_id,
            codeforge_version=self._config.name,
            run_mode=run_mode_typed,
            started_at=_now(),
            status="running",
            config_snapshot=self._config.to_dict(),
            retry_counters=RetryCounters(),
            agent_call_count=0,
        )

        self._pending = PendingWrites(self._project_state)
        self._event_log = EventLog(run_log_dir, run_id, self._config.name)
        self._validator = OutputValidator(self._config.to_dict())
        self._gates = GateEvaluator(self._validator, self._event_log, self._config.to_dict())
        self._router = ModelRouter(self._config)

        manifest = load_manifest()
        self._manifest = manifest
        repos = self._config.repos
        self._assembler = ContextAssembler(
            manifest=manifest,
            artifact_store=self.artifact_store,
            project_state=self._project_state,
            pending_writes=self._pending,
            run_log_dir=run_log_dir,
            source_root=Path(repos.source_code.path) if repos and repos.source_code.path else None,
        )

        self._event_log.update_run_snapshot(self._run)
        return self._run

    def resume_run(self, run: CodeforgeRun) -> None:
        """Restore state from a persisted CodeforgeRun (codeforge resume command)."""
        self._run = run
        self._run.config_snapshot = self._config.to_dict()
        run_log_dir = self._run_log_dir / run.run_id
        self._artifact_store = ArtifactStore(run_log_dir)
        self._pending = PendingWrites(self._project_state)
        self._event_log = EventLog(run_log_dir, run.run_id, self._config.name)
        self._validator = OutputValidator(self._config.to_dict())
        self._gates = GateEvaluator(self._validator, self._event_log, self._config.to_dict())
        self._router = ModelRouter(self._config)
        manifest = load_manifest()
        self._manifest = manifest
        repos = self._config.repos
        self._assembler = ContextAssembler(
            manifest=manifest,
            artifact_store=self.artifact_store,
            project_state=self._project_state,
            pending_writes=self._pending,
            run_log_dir=run_log_dir,
            source_root=Path(repos.source_code.path) if repos and repos.source_code.path else None,
        )

    # ------------------------------------------------------------------
    # Properties (convenience accessors with assertion)
    # ------------------------------------------------------------------

    @property
    def run(self) -> CodeforgeRun:
        assert self._run is not None, "start_run() must be called first"
        return self._run

    @property
    def pending(self) -> PendingWrites:
        assert self._pending is not None
        return self._pending

    @property
    def event_log(self) -> EventLog:
        assert self._event_log is not None
        return self._event_log

    @property
    def gates(self) -> GateEvaluator:
        assert self._gates is not None
        return self._gates

    @property
    def router(self) -> ModelRouter:
        assert self._router is not None
        return self._router

    @property
    def assembler(self) -> ContextAssembler:
        assert self._assembler is not None
        return self._assembler

    @property
    def manifest(self) -> FirewallManifest:
        assert self._manifest is not None
        return self._manifest

    @property
    def artifact_store(self) -> ArtifactStore:
        assert (
            self._artifact_store is not None
        ), "start_run()/resume_run() must be called first"
        return self._artifact_store

    # ------------------------------------------------------------------
    # Counter helpers
    # ------------------------------------------------------------------

    def _counters_snap(self) -> CountersSnapshot:
        return CountersSnapshot(
            **self.run.retry_counters.model_dump(),
            agent_call_count=self.run.agent_call_count,
        )

    def _inject_stack_guidance(self, pkg: Any, agent_id: str) -> None:
        """Inject the active stack profile's prompt fragment into an agent's context.

        Mirrors the other orchestrator-managed pseudo-state fields (_run_mode, etc.):
        the agent reads `state.get("_stack_guidance")` in build_user_turn. Agents not in
        the mapping (requirements_analyst, test_analyst) are stack-agnostic and skipped.
        """
        fragment_key = _STACK_FRAGMENT_FOR_AGENT.get(agent_id)
        if fragment_key is None:
            return
        fragment = self._config.stack_profile.prompt_fragment(fragment_key)
        pkg.state_documents["_stack_guidance"] = fragment or ""

    def _apply_outcome(self, outcome: RoutingOutcome) -> None:
        """Apply counter deltas and resets from a routing outcome."""
        new_counters = apply_outcome(self.run.retry_counters, outcome)
        self.run.retry_counters = new_counters

        self.event_log.emit_routing(
            routing_table_row=outcome.row_id,
            decision=outcome.decision,
            next_state=outcome.next_state,
            counters=self._counters_snap(),
            counter_deltas=outcome.counter_deltas,
            counter_resets=outcome.counter_resets,
            detail=outcome.detail,
        )
        self.event_log.update_run_snapshot(self.run)

    def _escalate(self, reason: EscalationReason, context: str = "") -> NoReturn:
        """Record escalation, update run status, raise EscalationError."""
        event = EscalationEvent(
            escalation_id=str(uuid.uuid4()),
            triggered_at=_now(),
            reason=reason,
            agent_output_ref=context,
            resolved=False,
            suggested_reentry_state=self._current_phase,
        )
        self.run.escalations.append(event)
        self.run.status = "failed_escalated"
        self.event_log.update_run_snapshot(self.run)
        raise EscalationError(reason, context, phase=self._current_phase)

    # ------------------------------------------------------------------
    # Agent invocation (with pre/post event emission)
    # ------------------------------------------------------------------

    def _build_tool_executor(
        self, agent_id: str, assembly_id: str | None
    ) -> ToolExecutor | None:
        """Build a read-only tool executor for tool-eligible continuation invocations.

        Returns None (no tools) unless ALL of: run_mode is continuation, the agent
        is in the firewall tool allowlist, and a source repo path is configured.
        The blind set (test_designer, test_analyst, requirements_analyst) never
        passes the allowlist check, so they are never handed tools.
        """
        if self.run.run_mode != "continuation":
            return None
        if not self.manifest.tools_enabled_for(cast(LogActor, agent_id)):
            return None
        repos = self._config.repos
        if repos is None or not repos.source_code.path:
            return None

        return ToolExecutor(
            root=Path(repos.source_code.path),
            agent_id=cast(LogActor, agent_id),
            manifest=self.manifest,
            event_log=self.event_log,
            counters=self._counters_snap(),
            assembly_id=assembly_id or "",
            max_tool_turns=self._config.tools.max_tool_turns,
        )

    def _invoke_agent(
        self,
        agent_id: str,
        system_prompt: str,
        user_turn: str,
        invocation_type: str = "first",
        assembly_id: str | None = None,
        reprompt_reason: str | None = None,
        stripped_fields: list[str] | None = None,
        context_package: ContextPackage | None = None,
    ) -> str:
        """
        Pre-invocation ceiling check → handoff event → LLM call → return raw string.
        Increments agent_call_count. Does NOT validate the response.

        For tool-eligible continuation invocations a read-only tool loop runs
        inside the single LLM call; the whole loop counts as ONE agent invocation
        for the global ceiling. Tool AccessEvents are appended to context_package
        (when provided) and the package is re-persisted for audit completeness.
        """
        typed_agent_id = cast(AgentId, agent_id)
        typed_actor = cast(LogActor, agent_id)
        typed_invocation = cast(HandoffInvocationType, invocation_type)

        # Pre-invocation ceiling check (global agent-call ceiling)
        if not self.gates.check_global_ceiling(
            self.run.agent_call_count, self._counters_snap()
        ):
            outcome = route_ceiling_exceeded()
            self._apply_outcome(outcome)
            self._escalate("global_ceiling_exceeded", agent_id)

        # Read-only codebase tools (continuation + tool-enabled agents only).
        executor = self._build_tool_executor(agent_id, assembly_id)
        tools = executor.tool_schemas() if executor is not None else None

        # LLM call
        result = self.router.complete(
            agent_id=typed_agent_id,
            system_prompt=system_prompt,
            user_turn=user_turn,
            run_id=self.run.run_id,
            tools=tools,
            tool_executor=executor if tools else None,
        )

        # Persist tool reads into the context package audit surface.
        if executor is not None and executor.access_events and context_package is not None:
            context_package.access_events.extend(executor.access_events)
            self.assembler.persist(context_package)

        self.run.agent_call_count += 1

        # Single handoff event emitted after the call so litellm_call_id is available.
        self.event_log.emit_handoff(
            to_agent=typed_actor,
            invocation_type=typed_invocation,
            counters=self._counters_snap(),
            assembly_id=assembly_id,
            stripped_fields=stripped_fields,
            reprompt_reason=reprompt_reason,
            litellm_call_id=result.litellm_call_id,
        )

        self._last_truncated = result.truncated
        if result.truncated:
            logger.warning(
                "Agent '%s' returned finish_reason=length (%d bytes); "
                "routing through the bounded truncation path "
                "(one retry for a transient hiccup, then escalate output_truncated)",
                agent_id, len(result.content.encode()),
            )

        return result.content

    def _store_artifact(
        self,
        artifact_type: str,
        agent_id: str,
        output: Any,
    ) -> ArtifactRef:
        """Write a validated artifact to the artifact store and return a ref."""
        from codeforge.firewall.manifest import load_manifest as _load
        typed_artifact_type = cast(ArtifactType, artifact_type)
        typed_agent_id = cast(AgentId, agent_id)

        manifest = _load()
        access = manifest.get_artifact_access(typed_artifact_type)
        allowed = list(access.allowed_consumers if access else [])
        forbidden = list(access.forbidden_consumers if access else [])

        meta = self.artifact_store.write(
            artifact_type=typed_artifact_type,
            produced_by=typed_agent_id,
            output=output,
            run_id=self.run.run_id,
            codeforge_version=self._config.name,
            schema_version="1.0.0",
            allowed_consumers=allowed,
            forbidden_consumers=forbidden,
        )
        return ArtifactRef(
            artifact_id=meta.artifact_id,
            artifact_type=meta.artifact_type,
            stored_at=meta.created_at,
            content_hash=meta.content_hash,
            schema_version=meta.schema_version,
        )

    # ------------------------------------------------------------------
    # Policy gate failure handling (block flag / low confidence)
    # ------------------------------------------------------------------

    def _handle_policy_escalation(
        self,
        gate_result: GateResult,
        agent_id: str,
        artifact_type: str,
        low_confidence_outcome: RoutingOutcome,
    ) -> RePromptContext:
        """
        Handle a terminal policy gate failure for an agent phase.

        - block_flag → always terminal: persist the output + emit a linked failing gate +
          escalate (raises).
        - low_confidence → one re-prompt (a 'be more thorough' nudge) before escalating.
          While in budget, returns a LowConfidenceRePrompt for the caller to re-prompt with
          (no artifact persisted — it's not terminal). Once exhausted, behaves like block_flag.

        On every terminal path the output is written to the ISOLATED failed-artifacts area
        (never artifacts/) so it can't leak into get_latest/exists; the only links to it are
        the gate event's artifact_ref and the escalation's agent_output_ref. This method
        returns only on the re-prompt path; all terminal paths raise EscalationError.
        """
        output = gate_result.parsed_output
        assert output is not None  # always set by GateEvaluator on a policy failure

        # Low confidence: try one re-prompt before escalating.
        if gate_result.escalation_reason == "low_confidence":
            cfg = self._config.to_dict()
            reprompt_outcome = route_low_confidence_reprompt(
                agent_id, self.run.retry_counters, cfg
            )
            if reprompt_outcome.decision == "re_prompt_same_agent":
                threshold = float(cfg.get("confidence_thresholds", {}).get(agent_id, 0.0))
                limit = int(cfg.get("retry_limits", {}).get("low_confidence_reprompt", 1))
                counter_field = f"{agent_id}_low_confidence_reprompt"
                attempt = getattr(self.run.retry_counters, counter_field, 0) + 1
                reprompt_outcome.detail = (
                    f"prior_confidence={output.confidence} threshold={threshold} "
                    f"(re-prompt {attempt}/{limit})"
                )
                # Log the gate failure + the fact that we're re-prompting (not persisted —
                # this output is not terminal).
                self.event_log.emit_gate(
                    rule=gate_result.policy_gate_rule,  # type: ignore[arg-type]
                    passed=False,
                    source_agent=cast(LogActor, agent_id),
                    counters=self._counters_snap(),
                    detail=self._format_policy_detail(gate_result, output) + " | re-prompting",
                )
                self._apply_outcome(reprompt_outcome)
                return LowConfidenceRePrompt(
                    prior_confidence=output.confidence,
                    threshold=threshold,
                    attempt_number=attempt,
                    max_attempts=limit,
                )
            # budget exhausted → fall through to terminal escalation below

        # Terminal: persist the offending output, emit a linked failing gate, escalate.
        artifact_id = self._write_failed_artifact(artifact_type, agent_id, output)
        self.event_log.emit_gate(
            rule=gate_result.policy_gate_rule,  # type: ignore[arg-type]
            passed=False,
            source_agent=cast(LogActor, agent_id),
            counters=self._counters_snap(),
            detail=self._format_policy_detail(gate_result, output),
            artifact_ref=artifact_id,
        )

        if gate_result.escalation_reason == "block_flag":
            self._apply_outcome(route_block_flag())
            self._escalate("block_flag", context=artifact_id)
        self._apply_outcome(low_confidence_outcome)
        self._escalate("low_confidence", context=artifact_id)

    def _write_failed_artifact(
        self,
        artifact_type: str,
        agent_id: str,
        output: Any,
    ) -> str:
        """Write a blocked/low-confidence output to failed_artifacts/ and return its id."""
        typed_artifact_type = cast(ArtifactType, artifact_type)
        typed_agent_id = cast(AgentId, agent_id)

        manifest = load_manifest()
        access = manifest.get_artifact_access(typed_artifact_type)
        allowed = list(access.allowed_consumers if access else [])
        forbidden = list(access.forbidden_consumers if access else [])

        meta = self.artifact_store.write_failed(
            artifact_type=typed_artifact_type,
            produced_by=typed_agent_id,
            output=output,
            run_id=self.run.run_id,
            codeforge_version=self._config.name,
            schema_version="1.0.0",
            allowed_consumers=allowed,
            forbidden_consumers=forbidden,
        )
        return meta.artifact_id

    def _persist_contract_failure(
        self,
        gate_result: GateResult,
        agent_id: str,
        artifact_type: str,
    ) -> str:
        """Persist a contract-violating (but schema-valid) output to failed_artifacts/.

        Contract escalations otherwise drop the offending output entirely. The parsed
        output is always set on a contract failure (GateEvaluator parses before contract
        checks), so we persist it and link it from the escalation's agent_output_ref.
        """
        output = gate_result.parsed_output
        assert output is not None  # always set by GateEvaluator on a contract failure
        return self._write_failed_artifact(artifact_type, agent_id, output)

    def _handle_structural_failure(
        self,
        raw: str,
        agent_id: str,
        gate_result: GateResult,
        truncated: bool | None = None,
    ) -> RePromptContext | None:
        """
        Handle a structural (malformed) or contract gate failure: route_malformed
        (or route_truncated when the response was cut off at max_tokens) decides
        re-prompt vs escalate.

        A response that hit finish_reason=length structurally cannot parse, so it lands
        here. It is routed through route_truncated — one bounded retry for a transient
        hiccup, then escalate as output_truncated — instead of spending the
        malformed_output budget on a re-prompt that regenerates the same oversized,
        identically-truncated output. `truncated` defaults to the most recent
        _invoke_agent result; pass it explicitly to drive the path in isolation.

        On the terminal (budget-exhausted) path the raw LLM response — which failed
        validation and would otherwise be dropped — is persisted to the ISOLATED
        raw_outputs/ area and linked from the escalation's agent_output_ref so the
        offending output is debuggable. Returns the re-prompt context on the re-prompt
        path; raises EscalationError on the terminal path.
        """
        reprompt = gate_result.malformed_reprompt or gate_result.violation_reprompt
        is_truncated = self._last_truncated if truncated is None else truncated
        config = self._config.to_dict()
        if is_truncated and not gate_result.structural_passed:
            outcome = route_truncated(self.run.retry_counters, config, agent_id)
        else:
            outcome = route_malformed(self.run.retry_counters, config, agent_id)
        if outcome.decision == "escalate":
            artifact_id = self.artifact_store.write_raw(raw, produced_by=agent_id)
            # Emit the failing schema_valid gate here (not in GateEvaluator) so it can link
            # the just-persisted raw output. Skipped on a contract-only failure (structural
            # passed) — that failing gate was already emitted by GateEvaluator.
            self._emit_structural_fail_gate(agent_id, gate_result, artifact_ref=artifact_id)
            self._apply_outcome(outcome)
            self._escalate(
                outcome.escalation_reason or "malformed_output", context=artifact_id
            )
        else:
            self._emit_structural_fail_gate(agent_id, gate_result, artifact_ref=None)
            self._apply_outcome(outcome)
        return reprompt

    def _emit_structural_fail_gate(
        self,
        agent_id: str,
        gate_result: GateResult,
        artifact_ref: str | None,
    ) -> None:
        """Emit the failing schema_valid gate event for a structural (malformed) failure.

        No-op when the failure was contract-only (structural passed): in that case the
        failing gate was already emitted by GateEvaluator. On a real structural failure
        this carries the persisted raw output's id as artifact_ref when escalating, so the
        events.jsonl gate line links straight to the dropped output.
        """
        if gate_result.structural_passed:
            return
        self.event_log.emit_gate(
            rule="schema_valid",
            passed=False,
            source_agent=cast(LogActor, agent_id),
            counters=self._counters_snap(),
            detail=gate_result.structural_detail or "",
            artifact_ref=artifact_ref,
        )

    def _format_policy_detail(self, gate_result: GateResult, output: Any) -> str:
        """
        Build a self-sufficient gate detail string so the events.jsonl line alone is
        enough to diagnose the escalation: the flag reason(s) and the problem summary.
        """
        parts = [f"escalation_reason={gate_result.escalation_reason}"]

        if gate_result.escalation_reason == "block_flag":
            for flag in output.unresolved_flags:
                if flag.severity != "block":
                    continue
                seg = f"flag={flag.id}: {flag.description}"
                if flag.suggested_action:
                    seg += f" (suggested: {flag.suggested_action})"
                parts.append(seg)
        else:
            parts.append(f"confidence={output.confidence}")

        payload = output.output
        summary = (
            payload.get("summary") if isinstance(payload, dict)
            else getattr(payload, "summary", None)
        )
        if isinstance(summary, str) and summary:
            if len(summary) > 300:
                summary = summary[:297] + "..."
            parts.append(f"summary={summary}")

        return " | ".join(parts)

    def _record_assumptions(self, output: Any, agent_id: AgentId) -> None:
        """Append recordable assumptions from agent output to assumptions_log."""
        from codeforge.schemas.contracts import AssumptionEntry
        entries = [
            AssumptionEntry(
                id=a.id,
                description=a.description,
                impact=a.impact,
                record=True,
                run_id=self.run.run_id,
                source_agent=agent_id,
                status="open",
            ).model_dump()
            for a in output.assumptions_made
            if a.record
        ]
        if entries:
            self.pending.merge_append("assumptions_log", entries)

    # ------------------------------------------------------------------
    # Requirements
    # ------------------------------------------------------------------

    def run_requirements(
        self,
        human_brief: str,
        human_interface: Any,
        system_prompt: str,
    ) -> RequirementsDoc:
        """
        Drive requirements clarification to completion.
        Returns the confirmed RequirementsDoc.
        human_interface: object with ask_clarification(), confirm_requirements() methods.
        """
        self._current_phase = "requirements_clarification"
        from codeforge.agents.requirements_analyst import RequirementsAnalystAgent

        clarification_history: list[dict[str, Any]] = []
        confirm_rejection: dict[str, str] | None = None
        reprompt: RePromptContext | None = None

        while True:
            # Assemble context
            pkg = self.assembler.assemble("requirements_analyst", self.run.run_id)

            # Inject orchestrator-managed fields for build_user_turn()
            pkg.state_documents["_run_mode"] = self.run.run_mode
            pkg.state_documents["_human_brief"] = human_brief
            pkg.state_documents["_clarification_history"] = json.dumps(clarification_history)
            pkg.state_documents["_confirm_rejection"] = json.dumps(confirm_rejection)

            user_turn = RequirementsAnalystAgent(
                "requirements_analyst", self.router, self._config
            ).build_user_turn(pkg, reprompt)

            raw = self._invoke_agent(
                "requirements_analyst", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
                reprompt_reason=reprompt.reason if reprompt is not None else None,
            )

            # Validate
            gate_result = self.gates.evaluate(
                raw=raw,
                expected_model=AgentOutput,
                agent_id="requirements_analyst",
                attempt_number=self.run.retry_counters.malformed_output,
                assembly_id=pkg.assembly_id,
                counters=self.run.retry_counters,
                agent_call_count=self.run.agent_call_count,
            )

            if not gate_result.structural_passed or not gate_result.contract_passed:
                reprompt = self._handle_structural_failure(
                    raw, "requirements_analyst", gate_result
                )
                continue

            if not gate_result.policy_passed:
                reprompt = self._handle_policy_escalation(
                    gate_result, "requirements_analyst", "requirements_doc",
                    route_low_confidence("requirements_analyst"),
                )
                continue

            # Parse output
            data = json.loads(raw)
            output: AgentOutput[Any] = AgentOutput(**data)

            if output.output.get("status") == "needs_clarification":
                questions = output.output.get("questions", [])
                answers = human_interface.ask_clarification(questions)
                clarification_history.append({
                    "round": len(clarification_history) + 1,
                    "questions": questions,
                    "answers": [a.model_dump() if hasattr(a, 'model_dump') else a for a in answers],
                })
                reprompt = None  # fresh round after human input
                outcome = route_requirements_clarify()
                self._apply_outcome(outcome)
                continue

            # status == "complete"
            req_doc_data = output.output.get("requirements_doc", {})

            # Human confirm gate
            outcome = route_requirements_complete()
            self._apply_outcome(outcome)

            confirmed = human_interface.confirm_requirements(req_doc_data)
            if confirmed:
                self.pending.set("requirements_history", req_doc_data)
                ref = self._store_artifact("requirements_doc", "requirements_analyst", output)
                self.run.artifacts["requirements_doc"] = ref
                self._record_assumptions(output, "requirements_analyst")
                outcome = route_requirements_confirmed()
                self._apply_outcome(outcome)
                return RequirementsDoc(**req_doc_data)
            else:
                feedback = human_interface.get_rejection_feedback()
                confirm_rejection = {
                    "rejected_doc_ref": "pending",
                    "rejection_feedback": feedback,
                }
                reprompt = None  # rejection context is in confirm_rejection field
                outcome = route_requirements_rejected()
                self._apply_outcome(outcome)
                continue

    # ------------------------------------------------------------------
    # Architecture
    # ------------------------------------------------------------------

    def run_architecture(
        self,
        requirements_doc: RequirementsDoc,
        system_prompt: str,
        human_interface: Any,
        spec_gap_context: dict[str, Any] | None = None,
    ) -> ArchitectureDoc:
        """Drive architecture design to completion."""
        self._current_phase = "architecture"
        from codeforge.agents.architecture_designer import ArchitectureDesignerAgent
        from codeforge.schemas.contracts import InterfaceManifest

        reprompt: RePromptContext | None = None

        while True:
            pkg = self.assembler.assemble("architecture_designer", self.run.run_id)

            # Inject orchestrator-managed fields for build_user_turn()
            self._inject_stack_guidance(pkg, "architecture_designer")
            pkg.state_documents["_run_mode"] = self.run.run_mode
            pkg.state_documents["_spec_gap_context"] = json.dumps(
                spec_gap_context if spec_gap_context is not None else None
            )

            user_turn = ArchitectureDesignerAgent(
                "architecture_designer", self.router, self._config
            ).build_user_turn(pkg, reprompt)

            raw = self._invoke_agent(
                "architecture_designer", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
                reprompt_reason=reprompt.reason if reprompt is not None else None,
                context_package=pkg,
            )

            gate_result = self.gates.evaluate(
                raw=raw,
                expected_model=ArchitectureDesignerOutput,
                agent_id="architecture_designer",
                attempt_number=self.run.retry_counters.architecture_validation,
                assembly_id=pkg.assembly_id,
                counters=self.run.retry_counters,
                agent_call_count=self.run.agent_call_count,
                requirements_doc=requirements_doc,
            )

            if not gate_result.structural_passed:
                reprompt = self._handle_structural_failure(
                    raw, "architecture_designer", gate_result
                )
                continue

            if not gate_result.contract_passed:
                reprompt = gate_result.violation_reprompt
                outcome = route_architecture_invalid(self.run.retry_counters, self._config.to_dict())
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    artifact_id = self._persist_contract_failure(
                        gate_result, "architecture_designer", "architecture_doc"
                    )
                    self._escalate(
                        outcome.escalation_reason or "max_retries_exceeded",
                        context=artifact_id,
                    )
                continue

            if not gate_result.policy_passed:
                reprompt = self._handle_policy_escalation(
                    gate_result, "architecture_designer", "architecture_doc",
                    route_architecture_lowconf(),
                )
                continue

            output = cast("AgentOutput[Any]", gate_result.parsed_output)
            arch_doc = cast(ArchitectureDoc, output.output)

            # Check for locked tech decisions
            locked = [d for d in arch_doc.tech_decisions if d.locked]
            outcome = route_architecture_valid(has_locked_decisions=bool(locked))
            self._apply_outcome(outcome)

            if locked:
                confirmed = human_interface.confirm_tech_decisions(locked)
                if confirmed:
                    tech_data = {"schema_version": "1.0.0", "decisions": [
                        {**d.model_dump(), "run_id": self.run.run_id, "confirmed_at": _now()}
                        for d in locked
                    ]}
                    self.pending.set("tech_stack", tech_data)

            # Record tech decisions with record=True to decisions_log
            recordable_decisions = [d for d in arch_doc.tech_decisions if d.record]
            if recordable_decisions:
                self.pending.merge_append("decisions_log", [
                    {"entry_id": str(uuid.uuid4()), "run_id": self.run.run_id,
                     "entry_type": "agent_decision", "source_agent": "architecture_designer",
                     "decision": d.decision, "rationale": d.rationale,
                     "created_at": _now()}
                    for d in recordable_decisions
                ])

            # Store architecture in pending_writes
            self.pending.set("architecture", {
                "schema_version": "1.0.0",
                "last_updated_run": self.run.run_id,
                "modules": [m.model_dump() for m in arch_doc.modules],
                "interfaces": [i.model_dump() for i in arch_doc.interfaces],
                "data_flow": [f.model_dump() for f in arch_doc.data_flow],
            })

            # Store artifacts
            ref = self._store_artifact("architecture_doc", "architecture_designer", output)
            self.run.artifacts["architecture_doc"] = ref

            # Project interface_manifest from arch_doc + requirements_doc.
            iface_manifest = InterfaceManifest(
                interfaces=arch_doc.interfaces,
                data_contracts=requirements_doc.data_contracts,
                acceptance_criteria=requirements_doc.acceptance_criteria,
            )
            iface_output = AgentOutput(
                output=iface_manifest,
                assumptions_made=[],
                confidence=1.0,
                unresolved_flags=[],
            )
            iface_ref = self._store_artifact("interface_manifest", "orchestrator", iface_output)
            self.run.artifacts["interface_manifest"] = iface_ref

            self._record_assumptions(output, "architecture_designer")
            return arch_doc

    # ------------------------------------------------------------------
    # Coding (Implementation)
    # ------------------------------------------------------------------

    def run_coding(
        self,
        requirements_doc: RequirementsDoc,
        architecture_doc: ArchitectureDoc,
        system_prompt: str,
        retry_context: dict[str, Any] | None = None,
        code_fix_context: dict[str, Any] | None = None,
        entry_stripped_fields: list[str] | None = None,
        dep_fix_context: dict[str, Any] | None = None,
    ) -> CodeArtifact:
        """Drive coding to completion."""
        self._current_phase = "coding"
        from codeforge.agents.coder import CoderAgent

        reprompt: RePromptContext | None = None
        first_call = True

        while True:
            pkg = self.assembler.assemble("coder", self.run.run_id)

            # Inject orchestrator-managed fields for build_user_turn()
            self._inject_stack_guidance(pkg, "coder")
            pkg.state_documents["_run_mode"] = self.run.run_mode
            pkg.state_documents["_existing_interfaces"] = json.dumps([])
            pkg.state_documents["_retry_context"] = json.dumps(retry_context)
            pkg.state_documents["_code_fix_context"] = json.dumps(code_fix_context)
            pkg.state_documents["_dep_fix_context"] = json.dumps(dep_fix_context)

            user_turn = CoderAgent(
                "coder", self.router, self._config
            ).build_user_turn(pkg, reprompt)

            raw = self._invoke_agent(
                "coder", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
                reprompt_reason=reprompt.reason if reprompt is not None else None,
                # stripped_fields only applies to the entry invocation (describes what
                # the orchestrator stripped from the previous phase's review findings)
                stripped_fields=entry_stripped_fields if first_call else None,
                context_package=pkg,
            )
            first_call = False

            gate_result = self.gates.evaluate(
                raw=raw,
                expected_model=CoderOutput,
                agent_id="coder",
                attempt_number=self.run.retry_counters.coder_validation,
                assembly_id=pkg.assembly_id,
                counters=self.run.retry_counters,
                agent_call_count=self.run.agent_call_count,
                requirements_doc=requirements_doc,
            )

            if not gate_result.structural_passed:
                reprompt = self._handle_structural_failure(raw, "coder", gate_result)
                continue

            if not gate_result.contract_passed:
                reprompt = gate_result.violation_reprompt
                violation = gate_result.violation_reprompt
                if violation and violation.rule == "requirements_txt_present":
                    outcome = route_coding_no_requirements_txt(self.run.retry_counters, self._config.to_dict())
                else:
                    outcome = route_coding_ac_gap(self.run.retry_counters, self._config.to_dict())
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    artifact_id = self._persist_contract_failure(
                        gate_result, "coder", "code_artifact"
                    )
                    self._escalate(
                        outcome.escalation_reason or "max_retries_exceeded",
                        context=artifact_id,
                    )
                continue

            if not gate_result.policy_passed:
                reprompt = self._handle_policy_escalation(
                    gate_result, "coder", "code_artifact",
                    route_low_confidence("coder"),
                )
                continue

            output = cast("AgentOutput[Any]", gate_result.parsed_output)
            code_artifact = cast(CodeArtifact, output.output)

            outcome = route_coding_valid()
            self._apply_outcome(outcome)

            ref = self._store_artifact("code_artifact", "coder", output)
            self.run.artifacts["code_artifact"] = ref
            self._record_assumptions(output, "coder")

            mi_output: AgentOutput[ModuleInterfaces] = AgentOutput(
                output=code_artifact.module_interfaces,
                assumptions_made=[],
                confidence=1.0,
                unresolved_flags=[],
            )
            mi_ref = self._store_artifact("module_interfaces", "coder", mi_output)
            self.run.artifacts["module_interfaces"] = mi_ref

            return code_artifact

    # ------------------------------------------------------------------
    # Code review
    # ------------------------------------------------------------------

    def run_code_review(
        self,
        requirements_doc: RequirementsDoc,
        architecture_doc: ArchitectureDoc,
        code_artifact: CodeArtifact,
        system_prompt: str,
    ) -> ReviewReport:
        """Drive code review to completion. Returns passing ReviewReport."""
        self._current_phase = "code_review"
        from codeforge.agents.code_reviewer import CodeReviewerAgent

        reprompt: RePromptContext | None = None

        while True:
            pkg = self.assembler.assemble("code_reviewer", self.run.run_id)
            self._inject_stack_guidance(pkg, "code_reviewer")

            user_turn = CodeReviewerAgent(
                "code_reviewer", self.router, self._config
            ).build_user_turn(pkg, reprompt)

            raw = self._invoke_agent(
                "code_reviewer", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
                reprompt_reason=reprompt.reason if reprompt is not None else None,
                context_package=pkg,
            )

            gate_result = self.gates.evaluate(
                raw=raw,
                expected_model=CodeReviewerOutput,
                agent_id="code_reviewer",
                attempt_number=self.run.retry_counters.malformed_output,
                assembly_id=pkg.assembly_id,
                counters=self.run.retry_counters,
                agent_call_count=self.run.agent_call_count,
            )

            if not gate_result.structural_passed or not gate_result.contract_passed:
                reprompt = self._handle_structural_failure(raw, "code_reviewer", gate_result)
                continue

            if not gate_result.policy_passed:
                reprompt = self._handle_policy_escalation(
                    gate_result, "code_reviewer", "review_report",
                    route_low_confidence("code_reviewer"),
                )
                continue

            output = cast("AgentOutput[Any]", gate_result.parsed_output)
            report = cast(ReviewReport, output.output)

            if report.verdict == "fail":
                outcome = route_code_review_fail(self.run.retry_counters, self._config.to_dict())
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    self._escalate(outcome.escalation_reason or "max_retries_exceeded")
                # Caller must re-invoke coder with retry context
                raise _ReviewFailed("code_review", report)

            if report.verdict == "pass_with_notes":
                self.pending.merge_append("decisions_log", [
                    {"entry_id": str(uuid.uuid4()), "run_id": self.run.run_id,
                     "entry_type": "agent_decision", "source_agent": "code_reviewer",
                     "decision": report.summary, "rationale": "pass_with_notes",
                     "created_at": _now()}
                ])

            outcome = route_code_review_pass(report.verdict == "pass_with_notes")
            self._apply_outcome(outcome)
            ref = self._store_artifact("review_report", "code_reviewer", output)
            self.run.artifacts["review_report"] = ref
            self._record_assumptions(output, "code_reviewer")
            return report

    # ------------------------------------------------------------------
    # Security review
    # ------------------------------------------------------------------

    def run_security_review(
        self,
        requirements_doc: RequirementsDoc,
        code_artifact: CodeArtifact,
        system_prompt: str,
    ) -> SecurityReport:
        """Drive security review to completion. Returns passing SecurityReport."""
        self._current_phase = "code_review"
        from codeforge.agents.security_reviewer import SecurityReviewerAgent

        reprompt: RePromptContext | None = None

        while True:
            pkg = self.assembler.assemble("security_reviewer", self.run.run_id)
            self._inject_stack_guidance(pkg, "security_reviewer")

            user_turn = SecurityReviewerAgent(
                "security_reviewer", self.router, self._config
            ).build_user_turn(pkg, reprompt)

            raw = self._invoke_agent(
                "security_reviewer", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
                reprompt_reason=reprompt.reason if reprompt is not None else None,
                context_package=pkg,
            )

            gate_result = self.gates.evaluate(
                raw=raw,
                expected_model=SecurityReviewerOutput,
                agent_id="security_reviewer",
                attempt_number=self.run.retry_counters.malformed_output,
                assembly_id=pkg.assembly_id,
                counters=self.run.retry_counters,
                agent_call_count=self.run.agent_call_count,
            )

            if not gate_result.structural_passed or not gate_result.contract_passed:
                reprompt = self._handle_structural_failure(raw, "security_reviewer", gate_result)
                continue

            if not gate_result.policy_passed:
                reprompt = self._handle_policy_escalation(
                    gate_result, "security_reviewer", "security_report",
                    route_low_confidence("security_reviewer"),
                )
                continue

            output = cast("AgentOutput[Any]", gate_result.parsed_output)
            report = cast(SecurityReport, output.output)

            if report.verdict == "fail":
                outcome = route_security_review_fail(self.run.retry_counters, self._config.to_dict())
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    self._escalate(outcome.escalation_reason or "max_retries_exceeded")
                raise _ReviewFailed("security_review", report)

            if report.verdict == "pass_with_notes":
                self.pending.merge_append("decisions_log", [
                    {"entry_id": str(uuid.uuid4()), "run_id": self.run.run_id,
                     "entry_type": "agent_decision", "source_agent": "security_reviewer",
                     "decision": report.summary, "rationale": "pass_with_notes",
                     "created_at": _now()}
                ])

            outcome = route_security_review_pass(report.verdict == "pass_with_notes")
            self._apply_outcome(outcome)
            ref = self._store_artifact("security_report", "security_reviewer", output)
            self.run.artifacts["security_report"] = ref
            self._record_assumptions(output, "security_reviewer")
            return report

    # ------------------------------------------------------------------
    # Test design
    # ------------------------------------------------------------------

    def run_test_design(
        self,
        requirements_doc: RequirementsDoc,
        architecture_doc: ArchitectureDoc,
        system_prompt: str,
        code_fix_context: dict[str, Any] | None = None,
        retry_context: dict[str, Any] | None = None,
        env_fix_context: dict[str, Any] | None = None,
    ) -> TestSuite:
        """Drive test design to completion."""
        self._current_phase = "test_design"
        from codeforge.agents.test_designer import TestDesignerAgent

        reprompt: RePromptContext | None = None

        while True:
            # Budget check: test_loop must still have remaining budget.
            # Use > (strictly greater) so that a counter that was incremented to exactly
            # the limit by route_test_analysis_* is still allowed one test run; the
            # routing function is the authoritative decision point and already approved
            # the cycle. Only fire test_design_exhausted if the counter somehow exceeds
            # the limit (safety-net for unexpected re-entries).
            test_loop_limit = self._config.to_dict().get("retry_limits", {}).get("test_loop", 2)
            if self.run.retry_counters.test_loop > test_loop_limit:
                outcome = route_test_design_covmap_invalid(self.run.retry_counters, self._config.to_dict())
                self._apply_outcome(outcome)
                self._escalate(outcome.escalation_reason or "max_retries_exceeded")

            pkg = self.assembler.assemble("test_designer", self.run.run_id)

            # Inject orchestrator-managed fields for build_user_turn()
            self._inject_stack_guidance(pkg, "test_designer")
            pkg.state_documents["_code_fix_context"] = json.dumps(code_fix_context)
            pkg.state_documents["_retry_context"] = json.dumps(retry_context)
            pkg.state_documents["_env_fix_context"] = json.dumps(env_fix_context)

            user_turn = TestDesignerAgent(
                "test_designer", self.router, self._config
            ).build_user_turn(pkg, reprompt)

            raw = self._invoke_agent(
                "test_designer", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
                reprompt_reason=reprompt.reason if reprompt is not None else None,
            )

            gate_result = self.gates.evaluate(
                raw=raw,
                expected_model=TestDesignerOutput,
                agent_id="test_designer",
                attempt_number=self.run.retry_counters.test_loop,
                assembly_id=pkg.assembly_id,
                counters=self.run.retry_counters,
                agent_call_count=self.run.agent_call_count,
                requirements_doc=requirements_doc,
            )

            if not gate_result.structural_passed:
                reprompt = self._handle_structural_failure(raw, "test_designer", gate_result)
                continue

            if not gate_result.contract_passed:
                reprompt = gate_result.violation_reprompt
                outcome = route_test_design_covmap_invalid(self.run.retry_counters, self._config.to_dict())
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    artifact_id = self._persist_contract_failure(
                        gate_result, "test_designer", "test_suite"
                    )
                    self._escalate(
                        outcome.escalation_reason or "max_retries_exceeded",
                        context=artifact_id,
                    )
                continue

            if not gate_result.policy_passed:
                reprompt = self._handle_policy_escalation(
                    gate_result, "test_designer", "test_suite",
                    route_low_confidence("test_designer"),
                )
                continue

            output = cast("AgentOutput[Any]", gate_result.parsed_output)
            test_suite = cast(TestSuite, output.output)

            ref = self._store_artifact("test_suite", "test_designer", output)
            self.run.artifacts["test_suite"] = ref
            self._record_assumptions(output, "test_designer")

            # Emit the test_design_valid routing event (was previously missing from event log)
            self._apply_outcome(route_test_design_valid())

            return test_suite

    # ------------------------------------------------------------------
    # Test analysis
    # ------------------------------------------------------------------

    def run_test_analysis(
        self,
        requirements_doc: RequirementsDoc,
        test_suite: TestSuite,
        test_runner_results: dict[str, Any],
        system_prompt: str,
    ) -> TestAnalysis:
        """Drive test analysis to completion."""
        self._current_phase = "test_execution"
        from codeforge.agents.test_analyst import TestAnalystAgent

        reprompt: RePromptContext | None = None

        while True:
            pkg = self.assembler.assemble("test_analyst", self.run.run_id)

            # Inject test runner results — not in artifact store, passed directly
            pkg.state_documents["_test_runner_results"] = json.dumps(test_runner_results)

            user_turn = TestAnalystAgent(
                "test_analyst", self.router, self._config
            ).build_user_turn(pkg, reprompt)

            raw = self._invoke_agent(
                "test_analyst", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
                reprompt_reason=reprompt.reason if reprompt is not None else None,
            )

            gate_result = self.gates.evaluate(
                raw=raw,
                expected_model=TestAnalystOutput,
                agent_id="test_analyst",
                attempt_number=self.run.retry_counters.malformed_output,
                assembly_id=pkg.assembly_id,
                counters=self.run.retry_counters,
                agent_call_count=self.run.agent_call_count,
            )

            if not gate_result.structural_passed or not gate_result.contract_passed:
                reprompt = self._handle_structural_failure(raw, "test_analyst", gate_result)
                continue

            if not gate_result.policy_passed:
                reprompt = self._handle_policy_escalation(
                    gate_result, "test_analyst", "test_analysis",
                    route_low_confidence("test_analyst"),
                )
                continue

            output = cast("AgentOutput[Any]", gate_result.parsed_output)
            analysis = cast(TestAnalysis, output.output)

            ref = self._store_artifact("test_analysis", "test_analyst", output)
            self.run.artifacts["test_analysis"] = ref
            self._record_assumptions(output, "test_analyst")
            return analysis

    # ------------------------------------------------------------------
    # Commit (flush + CommitWriter)
    # ------------------------------------------------------------------

    def _stage_ui_design_update(self) -> None:
        """If this run built UI components, mark them as built in the ui_design document."""
        if self._run is None:
            return

        # Read ui_design_component_ids from the staged requirements_history entry.
        req_history = self.pending.get("requirements_history")
        if not req_history:
            return
        component_ids: list[str] | None = req_history.get("ui_design_component_ids")
        # None means the field was absent; [] means no components — both skip.
        if not component_ids:
            return

        # Load UIDesignState — pending first, then disk.
        from codeforge.schemas.contracts import UIDesignState
        pending_ui = self.pending.get("ui_design")
        if pending_ui is not None:
            ui_state = UIDesignState(**pending_ui)
        else:
            ui_state = self._project_state.load_ui_design()
        if ui_state is None:
            return  # not seeded; nothing to update

        # Update matching component statuses.
        id_set = set(component_ids)
        for comp in ui_state.components:
            if comp.id in id_set:
                comp.status = "built"
        ui_state.last_updated_run = self._run.run_id
        self.pending.set("ui_design", ui_state.model_dump())

    def run_commit(self) -> None:
        """Flush pending_writes to disk. CommitWriter invocation handled by CLI layer."""
        self._current_phase = "commit"
        self._stage_ui_design_update()
        counters = self._counters_snap()
        flush_pending_writes(
            self.pending,
            self._project_state,
            self.event_log,
            counters,
            run_id=self.run.run_id,
        )
        # Status is promoted to "succeeded" by the CLI only after the git commit
        # (_do_commit) actually lands. Marking success here would be premature: a
        # commit failure must leave the run as failed_escalated and resumable, not
        # falsely "succeeded".
        self.event_log.update_run_snapshot(self.run)

    # ------------------------------------------------------------------
    # Top-level orchestration
    # ------------------------------------------------------------------

    def _load_prompts(self) -> dict[str, str]:
        """Read rendered system prompts from config/prompts/rendered/."""
        from pathlib import Path as _Path
        rendered_dir = _Path(__file__).parent.parent / "config" / "prompts" / "rendered"
        agent_ids = [
            "requirements_analyst",
            "architecture_designer",
            "coder",
            "code_reviewer",
            "security_reviewer",
            "test_designer",
            "test_analyst",
        ]
        prompts: dict[str, str] = {}
        for aid in agent_ids:
            p = rendered_dir / f"{aid}.md"
            if not p.exists():
                raise FileNotFoundError(
                    f"Rendered prompt not found: {p}. "
                    "Run `python -m codeforge.config.prompts.build` to build prompts."
                )
            prompts[aid] = p.read_text(encoding="utf-8")
        return prompts

    def _load_artifact_output(self, artifact_type: str) -> dict[str, Any] | None:
        """
        Load the latest artifact of the given type from the artifact store.
        Returns the .output dict, or None if not found.
        """
        from typing import cast
        from codeforge.schemas.contracts import ArtifactType as _AT
        output = self.artifact_store.get_latest(cast(_AT, artifact_type))
        if output is None:
            return None
        return dict(output.output) if isinstance(output.output, dict) else output.output

    def execute(
        self,
        human_brief: str,
        human_interface: Any,
        initial_state: str = "requirements",
    ) -> "tuple[RequirementsDoc, CodeArtifact, TestSuite]":
        """
        Drive phases 1–6. CommitWriter is handled by the CLI caller.

        initial_state: one of 'requirements', 'architecture', 'coding',
        'test_design', 'test_execution'. Use any value other than 'requirements'
        when resuming a prior run — the required artifacts are loaded from the
        artifact store for the resumed run.
        """
        from codeforge.agents.test_runner import InfrastructureError, TestRunner
        from codeforge.schemas.contracts import TestRunnerInput

        prompts = self._load_prompts()

        spec_gap_context: dict[str, Any] | None = None
        code_fix_context: dict[str, Any] | None = None
        test_fix_context: dict[str, Any] | None = None
        env_fix_context: dict[str, Any] | None = None
        dep_fix_context: dict[str, Any] | None = None
        req_doc: RequirementsDoc | None = None
        arch_doc: ArchitectureDoc | None = None
        code_art: CodeArtifact | None = None
        test_suite: TestSuite | None = None
        runner_results: Any = None

        if initial_state in ("requirements", "requirements_clarification"):
            req_doc = self.run_requirements(human_brief, human_interface, prompts["requirements_analyst"])
            next_state = "architecture"
        else:
            # Resuming from a mid-run escalation — load persisted artifacts.
            req_data = self._load_artifact_output("requirements_doc")
            if req_data is None:
                raise RuntimeError("Cannot resume: requirements_doc artifact not found")
            # The requirements_doc artifact stores the analyst envelope
            # {status, requirements_doc: {...}}; the actual RequirementsDoc fields
            # are nested one level down (the live run returns the unwrapped doc).
            if "requirements_doc" in req_data:
                req_data = req_data["requirements_doc"]
            req_doc = RequirementsDoc(**req_data)

            if initial_state in ("coding", "code_review", "test_design", "test_execution", "commit"):
                arch_data = self._load_artifact_output("architecture_doc")
                if arch_data is not None:
                    arch_doc = ArchitectureDoc(**arch_data)

            # "commit" re-entry skips the phase loop (mapped to "done" below) but still
            # needs code_art + test_suite for run_commit's closing asserts and the CLI's
            # source-code commit.
            # "test_design" re-entry also needs code_art: test_design is bypassed on
            # resume so _run_impl_with_reviews never runs, leaving code_art=None. Without
            # it the subsequent test_execution asserts immediately with a blank error.
            if initial_state in ("test_design", "test_execution", "commit"):
                code_data = self._load_artifact_output("code_artifact")
                if code_data is not None:
                    code_art = CodeArtifact(**code_data)
            if initial_state in ("test_execution", "commit"):
                suite_data = self._load_artifact_output("test_suite")
                if suite_data is not None:
                    test_suite = TestSuite(**suite_data)

            # Map reentry_state names to the while-loop state labels
            _state_map = {
                "requirements_clarification": "requirements",
                "architecture": "architecture",
                "coding": "coding",
                "code_review": "coding",  # code_review re-enters coding loop
                "test_design": "test_design",
                "test_execution": "test_execution",
                "commit": "done",          # commit is handled by CLI after execute()
            }
            next_state = _state_map.get(initial_state, initial_state)

            # Re-entering the coding phase: restore the coder's per-invocation
            # retry cushions so failures accumulated in a prior session don't
            # consume retries the coder would have on a fresh coding invocation.
            # Both "coding" and "code_review" reentry states map to next_state
            # "coding" and trigger a fresh coder call, so both need the reset.
            # For "code_review" reentry specifically: the coder already produced a
            # passing artifact before code review escalated — resetting intentionally
            # grants a full budget for what is genuinely a new coding invocation.
            if next_state == "coding":
                self.run.retry_counters = self.run.retry_counters.model_copy(
                    update={
                        "malformed_output": 0,
                        "truncation_retry": 0,
                        "coder_low_confidence_reprompt": 0,
                    }
                )

            # Re-entering test_design (e.g. after a test_designer malformed_output
            # escalation): malformed_output is a shared, run-wide counter the prior
            # session left exhausted, so without this reset the first re-prompt would
            # immediately re-escalate. Mirror the coding reset for test_designer's
            # per-invocation cushions.
            if next_state == "test_design":
                self.run.retry_counters = self.run.retry_counters.model_copy(
                    update={
                        "malformed_output": 0,
                        "truncation_retry": 0,
                        "test_designer_low_confidence_reprompt": 0,
                    }
                )

        while next_state != "done":
            if next_state == "architecture":
                assert arch_doc is not None or next_state == "architecture"
                arch_doc = self.run_architecture(
                    req_doc, prompts["architecture_designer"], human_interface,
                    spec_gap_context=spec_gap_context,
                )
                spec_gap_context = None
                next_state = "coding"

            elif next_state == "coding":
                assert arch_doc is not None
                code_art = self._run_impl_with_reviews(
                    req_doc, arch_doc, prompts, human_interface, code_fix_context,
                    dep_fix_context=dep_fix_context,
                )
                dep_fix_context = None  # consumed; clear after coding completes
                next_state = "test_design"

            elif next_state == "test_design":
                assert arch_doc is not None
                test_suite = self.run_test_design(
                    req_doc, arch_doc, prompts["test_designer"],
                    code_fix_context=code_fix_context,
                    retry_context=test_fix_context,
                    env_fix_context=env_fix_context,
                )
                code_fix_context = None   # consumed; clear after test design completes
                test_fix_context = None   # consumed; clear after test design completes
                env_fix_context = None    # consumed; clear after test design completes
                next_state = "test_execution"

            elif next_state == "test_execution":
                self._current_phase = "test_execution"
                assert code_art is not None
                assert test_suite is not None
                runner = TestRunner(self._config)
                repos = self._config.repos
                source_root = (
                    repos.source_code.path
                    if repos is not None and self.run.run_mode == "continuation"
                    else None
                )
                try:
                    runner_results = runner.run(
                        TestRunnerInput(
                            test_suite=test_suite,
                            code_artifact=code_art,
                            run_config={},
                            run_mode=self.run.run_mode,
                            source_root=source_root,
                        )
                    )
                    next_state = "test_analysis"
                except InfrastructureError as exc:
                    outcome = route_test_execution_error(
                        self.run.retry_counters, self._config.to_dict()
                    )
                    self._apply_outcome(outcome)
                    if outcome.decision == "escalate":
                        self._escalate(outcome.escalation_reason or "human_required", str(exc))
                    # retry: stay in test_execution

            elif next_state == "test_analysis":
                assert test_suite is not None
                assert runner_results is not None
                analysis = self.run_test_analysis(
                    req_doc, test_suite, runner_results.model_dump(), prompts["test_analyst"]
                )
                verdict = analysis.verdict

                if verdict == "pass":
                    outcome = route_test_analysis_pass()
                    self._apply_outcome(outcome)

                    # Write test_coverage_map from analysis.coverage_update
                    coverage_entries = [
                        {**entry, "run_id": self.run.run_id}
                        for entry in analysis.coverage_update
                    ]
                    existing_cov = self.pending.get("test_coverage_map") or \
                        self._project_state.read("test_coverage_map") or \
                        {"schema_version": "1.0.0", "entries": []}
                    merged_entries = existing_cov.get("entries", []) + coverage_entries
                    self.pending.set("test_coverage_map", {
                        "schema_version": "1.0.0",
                        "entries": merged_entries,
                    })

                    # Update feature_registry: set this feature's status to "tested"
                    existing_reg = self.pending.get("feature_registry") or \
                        self._project_state.read("feature_registry") or \
                        {"schema_version": "1.0.0", "features": []}
                    features = existing_reg.get("features", [])
                    updated = False
                    for feat in features:
                        if feat.get("feature_title") == req_doc.feature_title:
                            feat["status"] = "tested"
                            feat["last_modified_run"] = self.run.run_id
                            updated = True
                            break
                    if not updated:
                        # New feature — create entry
                        features.append({
                            "feature_title": req_doc.feature_title,
                            "introduced_run": self.run.run_id,
                            "last_modified_run": self.run.run_id,
                            "status": "tested",
                            "interfaces": [],
                        })
                    self.pending.set("feature_registry", {
                        **existing_reg,
                        "features": features,
                    })

                    next_state = "done"

                elif verdict == "fail_code_bug":
                    outcome = route_test_analysis_code_bug(self.run.retry_counters, self._config.to_dict())
                    self._apply_outcome(outcome)
                    if outcome.decision == "escalate":
                        self._escalate(outcome.escalation_reason or "max_retries_exceeded")
                    code_fix_context = _build_code_fix_context(analysis, test_suite)
                    next_state = "coding"

                elif verdict == "fail_test_bug":
                    outcome = route_test_analysis_test_bug(self.run.retry_counters, self._config.to_dict())
                    self._apply_outcome(outcome)
                    if outcome.decision == "escalate":
                        self._escalate(outcome.escalation_reason or "max_retries_exceeded")
                    test_fix_context = _build_test_fix_context(analysis, test_suite)
                    next_state = "test_design"

                elif verdict == "fail_spec_gap":
                    outcome = route_test_analysis_spec_gap(self.run.retry_counters, self._config.to_dict())
                    self._apply_outcome(outcome)
                    if outcome.decision == "escalate":
                        self._escalate(outcome.escalation_reason or "max_retries_exceeded")
                    spec_gap_context = _build_spec_gap_context(analysis)
                    next_state = "architecture"

                elif verdict == "fail_ambiguous":
                    outcome = route_test_analysis_ambiguous()
                    self._apply_outcome(outcome)
                    self._escalate(outcome.escalation_reason or "human_required")

                else:  # "error"
                    cfg = self._config.to_dict()
                    # Auto-recover off the runner's deterministic error_phase: route the
                    # failure to the agent that owns the fix (coder for runtime deps,
                    # test_designer for test infra) before re-running / escalating.
                    error_phase = getattr(runner_results, "error_phase", None)
                    recovery = route_test_analysis_recoverable_error(
                        error_phase, self.run.retry_counters, cfg
                    )
                    if recovery is not None:
                        # Enrich the routing log line with the actual failure text.
                        stderr_tail = getattr(runner_results, "stderr_tail", "") or ""
                        if stderr_tail:
                            recovery.detail = f"{recovery.detail} | {stderr_tail[:200]}"
                        self._apply_outcome(recovery)
                        if recovery.decision == "escalate":
                            self._escalate(recovery.escalation_reason or "human_required")
                        if recovery.next_state == "coding":
                            dep_fix_context = _build_dep_fix_context(runner_results, prev_code_art=code_art)
                            next_state = "coding"
                        else:  # test_design
                            env_fix_context = _build_env_fix_context(analysis)
                            next_state = "test_design"
                    else:
                        outcome = route_test_analysis_error(self.run.retry_counters, cfg)
                        self._apply_outcome(outcome)
                        if outcome.decision == "escalate":
                            self._escalate(outcome.escalation_reason or "human_required")
                        next_state = "test_execution"

        self.run_commit()
        assert code_art is not None
        assert test_suite is not None
        return req_doc, code_art, test_suite

    def _run_impl_with_reviews(
        self,
        req_doc: RequirementsDoc,
        arch_doc: ArchitectureDoc,
        prompts: dict[str, str],
        human_interface: Any,
        code_fix_context: dict[str, Any] | None = None,
        dep_fix_context: dict[str, Any] | None = None,
    ) -> CodeArtifact:
        """Inner loop: coder → code review → security review. Retries on review failure."""
        retry_context: dict[str, Any] | None = None
        stripped_fields: list[str] | None = None
        cfg = self._config.to_dict()
        while True:
            code_art = self.run_coding(
                req_doc, arch_doc, prompts["coder"], retry_context, code_fix_context,
                entry_stripped_fields=stripped_fields,
                dep_fix_context=dep_fix_context,
            )
            code_fix_context = None  # only applies to the first coding attempt
            dep_fix_context = None   # only applies to the first coding attempt
            stripped_fields = None

            try:
                self.run_code_review(req_doc, arch_doc, code_art, prompts["code_reviewer"])
            except _ReviewFailed as exc:
                # Whitelist projection: only description + suggested_fix per finding.
                projected = [
                    {"description": f.description, "suggested_fix": f.suggested_fix}
                    for f in exc.report.findings
                ]
                stripped_fields = ["id", "file", "line_range", "category", "severity"]
                retry_context = {
                    "retry_number": self.run.retry_counters.code_review_loop,
                    "max_retries": cfg.get("retry_limits", {}).get("code_review_loop", 3),
                    "trigger": "code_review_fail",
                    "review_findings": projected,
                    "security_findings": [],
                    "code_bug_findings": [],
                }
                continue

            try:
                self.run_security_review(req_doc, code_art, prompts["security_reviewer"])
            except _ReviewFailed as exc:
                projected = [
                    {"description": f.description, "suggested_fix": f.recommended_fix}
                    for f in exc.report.findings
                ]
                stripped_fields = ["id", "file", "line_range", "category", "severity", "cwe"]
                retry_context = {
                    "retry_number": self.run.retry_counters.security_review_loop,
                    "max_retries": cfg.get("retry_limits", {}).get("security_review_loop", 3),
                    "trigger": "security_review_fail",
                    "review_findings": [],
                    "security_findings": projected,
                    "code_bug_findings": [],
                }
                continue

            return code_art

    # ------------------------------------------------------------------
    # Terminal
    # ------------------------------------------------------------------

    def mark_failed_terminal(self) -> None:
        self.run.status = "failed_terminal"
        self.event_log.update_run_snapshot(self.run)


# ---------------------------------------------------------------------------
# Internal exception used to signal review failures back to the review loops
# ---------------------------------------------------------------------------

class _ReviewFailed(Exception):
    """Raised by phase4a/4b when verdict is fail — caught by the review loop."""

    def __init__(self, kind: str, report: Any) -> None:
        self.kind = kind
        self.report = report
        super().__init__(f"{kind} review failed")


# ---------------------------------------------------------------------------
# Module-level context builders
# ---------------------------------------------------------------------------

def _build_code_fix_context(
    analysis: "TestAnalysis", test_suite: "TestSuite"
) -> dict[str, Any]:
    """
    Build the code_fix_context dict passed back to the coder after a test failure.

    Per CodeFixContext schema: only flagged_criterion_ids. No test content, no summaries.
    Criterion ids are collected from the TestCase entries for each code_bug failure.
    """
    failed_tc_ids = {
        fa.test_case_id
        for fa in analysis.failure_analyses
        if fa.root_cause_hypothesis == "code_bug"
    }
    # Map test_case_id → criterion_ids from the test suite
    tc_map = {tc.id: tc.criterion_ids for tc in test_suite.test_cases}
    flagged: set[str] = set()
    for tc_id in failed_tc_ids:
        flagged.update(tc_map.get(tc_id, []))
    return {"flagged_criterion_ids": sorted(flagged)}


def _build_test_fix_context(
    analysis: "TestAnalysis", test_suite: "TestSuite"
) -> dict[str, Any]:
    """
    Build the retry_context dict passed back to the test_designer after a fail_test_bug verdict.

    Firewall-safe whitelist projection (gate: test_bug_context_clean) — only
    recommended_action and file_paths cross over. evidence is intentionally excluded:
    it is the analyst's internal reasoning and may reference source file paths, line
    numbers, or implementation details that test_designer must never see.
    """
    tc_paths: dict[str, list[str]] = {
        tc.id: [code.path for code in tc.code]
        for tc in test_suite.test_cases
    }
    failed_cases = [
        {
            "test_case_id": fa.test_case_id,
            "file_paths": tc_paths.get(fa.test_case_id, []),
            "recommended_action": fa.recommended_action,
        }
        for fa in analysis.failure_analyses
        if fa.root_cause_hypothesis == "test_bug"
    ]
    return {
        "trigger": "test_bug",
        "test_summary": analysis.summary,
        "failed_test_cases": failed_cases,
    }


def _build_spec_gap_context(analysis: "TestAnalysis") -> dict[str, Any]:
    """Build the spec_gap_context dict passed to the architecture designer."""
    result: dict[str, Any] = {
        "trigger": "test_fail_spec_gap",
        "test_summary": analysis.summary,
        "failure_analyses": [fa.model_dump() for fa in analysis.failure_analyses],
    }
    return result


def _build_env_fix_context(analysis: "TestAnalysis") -> dict[str, Any]:
    """
    Build the env_fix_context dict passed back to the test_designer to repair an
    environment failure (e.g. a missing test-only dependency in requirements-test.txt).

    Firewall-safe whitelist projection — the test_designer is forbidden the raw
    test_analysis artifact, so only the analyst summary and the recommended_action /
    evidence of environment-classified failures cross over. Never the raw artifact,
    never any code.
    """
    return {
        "trigger": "test_error_environment",
        "test_summary": analysis.summary,
        "environment_findings": [
            {
                "recommended_action": fa.recommended_action,
                "evidence": fa.evidence,
            }
            for fa in analysis.failure_analyses
            if fa.root_cause_hypothesis == "environment"
        ],
    }


def _build_dep_fix_context(
    runner_results: Any,
    prev_code_art: CodeArtifact | None = None,
) -> dict[str, Any]:
    """
    Build the dep_fix_context passed back to the coder to repair a runner failure the coder
    owns: a runtime-dependency failure (bad/missing package in the manifest) or a build /
    type-check failure (e.g. `tsc --noEmit`). The trigger tells the coder which to fix.

    No firewall projection needed: the coder owns the dependency manifest and source, and the
    runner stderr is its own build output (install/compiler), not another agent's artifact.

    prev_code_art: the coder's most recent CodeArtifact. Its file list is included so the
    second-pass coder knows what it had already planned — tool reads return pre-change disk
    state and cannot reveal these planned-but-not-yet-committed files.
    """
    error_phase = getattr(runner_results, "error_phase", None)
    trigger = "build_error" if error_phase == "build_failed" else "runtime_dep_error"
    ctx: dict[str, Any] = {
        "trigger": trigger,
        "error_phase": error_phase,
        "stderr_tail": getattr(runner_results, "stderr_tail", ""),
    }
    if prev_code_art is not None:
        ctx["previous_files"] = [
            {"path": f.path, "change_type": f.change_type}
            for f in prev_code_art.files
        ]
    return ctx
