"""
orchestrator/state_machine.py — Pipeline state machine.

Owns the PipelineRun object. Drives the pipeline through all phases.
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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from codeforge.config.config_loader import ConfigSnapshot
from codeforge.firewall.assembler import ContextAssembler
from codeforge.firewall.manifest import load_manifest
from codeforge.model_router.router import ModelRouter
from codeforge.orchestrator.event_log import EventLog
from codeforge.orchestrator.gates import GateEvaluator
from codeforge.orchestrator.pending_writes import PendingWrites
from codeforge.orchestrator.routing import (
    RoutingOutcome,
    apply_outcome,
    route_malformed,
    route_block_flag,
    route_ceiling_exceeded,
    route_low_confidence,
    route_p1_clarify,
    route_p1_complete,
    route_p1_confirmed,
    route_p1_rejected,
    route_p2_valid,
    route_p2_invalid,
    route_p2_lowconf,
    route_p3_no_requirements_txt,
    route_p3_ac_gap,
    route_p3_valid,
    route_p4a_fail,
    route_p4a_pass,
    route_p4b_fail,
    route_p4b_pass,
    route_p5d_covmap_invalid,
    route_p5e_error,
    route_p5c_pass,
    route_p5c_code_bug,
    route_p5c_test_bug,
    route_p5c_spec_gap,
    route_p5c_ambiguous,
    route_p5c_analyst_error,
)
from codeforge.orchestrator.state_writer import flush_pending_writes
from codeforge.schemas.contracts import (
    AgentId,
    ArtifactRef,
    ArtifactType,
    CodeArtifact,
    CountersSnapshot,
    EscalationEvent,
    EscalationReason,
    HandoffInvocationType,
    LogActor,
    PipelineRun,
    PipelineStatus,
    RequirementsDoc,
    RetryCounters,
    ReviewReport,
    SecurityReport,
    TestAnalysis,
    ArchitectureDoc,
    TestSuite,
)
from codeforge.schemas.validation import OutputValidator
from codeforge.store.artifact_store import ArtifactStore
from codeforge.store.project_state import ProjectStateStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


class EscalationError(Exception):
    """Raised when the pipeline must escalate to a human."""

    def __init__(self, reason: EscalationReason, context: str = "") -> None:
        self.reason = reason
        self.context = context
        super().__init__(f"Pipeline escalated: {reason} — {context}")


class StateMachine:
    """
    Pipeline orchestrator state machine.

    One instance per pipeline run. Not thread-safe — runs sequentially.
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

        # Stores
        self._project_state = ProjectStateStore(project_dir)
        self._artifact_store = ArtifactStore(run_log_dir)

        # Core components (initialised in start_run)
        self._run: PipelineRun | None = None
        self._pending: PendingWrites | None = None
        self._event_log: EventLog | None = None
        self._validator: OutputValidator | None = None
        self._gates: GateEvaluator | None = None
        self._router: ModelRouter | None = None
        self._assembler: ContextAssembler | None = None

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def start_run(
        self,
        run_mode: str,
        human_brief: str,
    ) -> PipelineRun:
        """
        Initialise a new PipelineRun and all components.
        Returns the run object — caller can inspect status after execute().
        """
        run_id = _new_run_id()
        run_log_dir = self._run_log_dir / run_id
        run_log_dir.mkdir(parents=True, exist_ok=True)

        from typing import Literal as Lit
        run_mode_typed = cast(Lit["new_project", "continuation"], run_mode)
        self._run = PipelineRun(
            run_id=run_id,
            pipeline_version=self._config.pipeline,
            run_mode=run_mode_typed,
            started_at=_now(),
            status="running",
            config_snapshot=self._config.to_dict(),
            retry_counters=RetryCounters(),
            agent_call_count=0,
        )

        self._pending = PendingWrites(self._project_state)
        self._event_log = EventLog(run_log_dir, run_id, self._config.pipeline)
        self._validator = OutputValidator(self._config.to_dict())
        self._gates = GateEvaluator(self._validator, self._event_log, self._config.to_dict())
        self._router = ModelRouter(self._config)

        manifest = load_manifest()
        self._assembler = ContextAssembler(
            manifest=manifest,
            artifact_store=self._artifact_store,
            project_state=self._project_state,
            pending_writes=self._pending,
            run_log_dir=run_log_dir,
        )

        self._event_log.update_run_snapshot(self._run)
        return self._run

    def resume_run(self, run: PipelineRun) -> None:
        """Restore state from a persisted PipelineRun (pipeline resume command)."""
        self._run = run
        run_log_dir = self._run_log_dir / run.run_id
        self._pending = PendingWrites(self._project_state)
        self._event_log = EventLog(run_log_dir, run.run_id, self._config.pipeline)
        self._validator = OutputValidator(self._config.to_dict())
        self._gates = GateEvaluator(self._validator, self._event_log, self._config.to_dict())
        self._router = ModelRouter(self._config)
        manifest = load_manifest()
        self._assembler = ContextAssembler(
            manifest=manifest,
            artifact_store=self._artifact_store,
            project_state=self._project_state,
            pending_writes=self._pending,
            run_log_dir=run_log_dir,
        )

    # ------------------------------------------------------------------
    # Properties (convenience accessors with assertion)
    # ------------------------------------------------------------------

    @property
    def run(self) -> PipelineRun:
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

    # ------------------------------------------------------------------
    # Counter helpers
    # ------------------------------------------------------------------

    def _counters_snap(self) -> CountersSnapshot:
        return CountersSnapshot(
            **self.run.retry_counters.model_dump(),
            agent_call_count=self.run.agent_call_count,
        )

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
        )
        self.event_log.update_run_snapshot(self.run)

    def _escalate(self, reason: EscalationReason, context: str = "") -> None:
        """Record escalation, update run status, raise EscalationError."""
        event = EscalationEvent(
            escalation_id=str(uuid.uuid4()),
            triggered_at=_now(),
            reason=reason,
            agent_output_ref=context,
            resolved=False,
        )
        self.run.escalations.append(event)
        self.run.status = "failed_escalated"
        self.event_log.update_run_snapshot(self.run)
        raise EscalationError(reason, context)

    # ------------------------------------------------------------------
    # Agent invocation (with pre/post event emission)
    # ------------------------------------------------------------------

    def _invoke_agent(
        self,
        agent_id: str,
        system_prompt: str,
        user_turn: str,
        invocation_type: str = "first",
        assembly_id: str | None = None,
        reprompt_reason: str | None = None,
        stripped_fields: list[str] | None = None,
    ) -> str:
        """
        Pre-invocation ceiling check → handoff event → LLM call → return raw string.
        Increments agent_call_count. Does NOT validate the response.
        """
        typed_agent_id = cast(AgentId, agent_id)
        typed_actor = cast(LogActor, agent_id)
        typed_invocation = cast(HandoffInvocationType, invocation_type)

        # Pre-invocation ceiling check (X-ceiling)
        if not self.gates.check_global_ceiling(
            self.run.agent_call_count, self._counters_snap()
        ):
            outcome = route_ceiling_exceeded()
            self._apply_outcome(outcome)
            self._escalate("global_ceiling_exceeded", agent_id)

        # Handoff event (before call)
        self.event_log.emit_handoff(
            to_agent=typed_actor,
            invocation_type=typed_invocation,
            counters=self._counters_snap(),
            assembly_id=assembly_id,
            stripped_fields=stripped_fields,
            reprompt_reason=reprompt_reason,
        )

        # LLM call
        result = self.router.complete(
            agent_id=typed_agent_id,
            system_prompt=system_prompt,
            user_turn=user_turn,
            run_id=self.run.run_id,
        )

        self.run.agent_call_count += 1

        # Update handoff event with litellm_call_id (emitted as follow-up)
        self.event_log.emit_handoff(
            to_agent=typed_actor,
            invocation_type=typed_invocation,
            counters=self._counters_snap(),
            assembly_id=assembly_id,
            litellm_call_id=result.litellm_call_id,
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

        meta = self._artifact_store.write(
            artifact_type=typed_artifact_type,
            produced_by=typed_agent_id,
            output=output,
            run_id=self.run.run_id,
            pipeline_version=self._config.pipeline,
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
    # Phase 1 — Requirements
    # ------------------------------------------------------------------

    def run_phase1(
        self,
        human_brief: str,
        human_interface: Any,
        system_prompt: str,
    ) -> RequirementsDoc:
        """
        Drive Phase 1 (requirements clarification) to completion.
        Returns the confirmed RequirementsDoc.
        human_interface: object with ask_clarification(), confirm_requirements() methods.
        """
        from codeforge.schemas.contracts import (
            RequirementsNeedsClarification, RequirementsComplete, AgentOutput,
        )

        clarification_history: list[dict[str, Any]] = []
        confirm_rejection: dict[str, str] | None = None

        while True:
            # Assemble context
            pkg = self.assembler.assemble("requirements_analyst", self.run.run_id)

            # Build user turn
            user_turn = json.dumps({
                "run_mode": self.run.run_mode,
                "human_brief": human_brief,
                "clarification_history": clarification_history,
                "confirm_rejection": confirm_rejection,
                "project_state": None,
            })

            raw = self._invoke_agent(
                "requirements_analyst", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
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

            if not gate_result.layer1_passed:
                outcome = route_malformed(
                    self.run.retry_counters, self._config.to_dict(), "requirements_analyst"
                )
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    self._escalate(outcome.escalation_reason or "malformed_output")
                continue

            if not gate_result.layer3_passed:
                if gate_result.escalation_reason == "block_flag":
                    self._apply_outcome(route_block_flag())
                    self._escalate("block_flag")
                self._apply_outcome(route_low_confidence("requirements_analyst"))
                self._escalate("low_confidence")

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
                outcome = route_p1_clarify()
                self._apply_outcome(outcome)
                continue

            # status == "complete"
            req_doc_data = output.output.get("requirements_doc", {})

            # Human confirm gate
            outcome = route_p1_complete()
            self._apply_outcome(outcome)

            confirmed = human_interface.confirm_requirements(req_doc_data)
            if confirmed:
                self.pending.set("requirements_history", req_doc_data)
                outcome = route_p1_confirmed()
                self._apply_outcome(outcome)
                return RequirementsDoc(**req_doc_data)
            else:
                feedback = human_interface.get_rejection_feedback()
                confirm_rejection = {
                    "rejected_doc_ref": "pending",
                    "rejection_feedback": feedback,
                }
                outcome = route_p1_rejected()
                self._apply_outcome(outcome)
                continue

    # ------------------------------------------------------------------
    # Phase 2 — Architecture
    # ------------------------------------------------------------------

    def run_phase2(
        self,
        requirements_doc: RequirementsDoc,
        system_prompt: str,
        human_interface: Any,
        spec_gap_context: dict[str, Any] | None = None,
    ) -> ArchitectureDoc:
        """Drive Phase 2 (architecture design) to completion."""
        from codeforge.schemas.contracts import AgentOutput

        while True:
            pkg = self.assembler.assemble("architecture_designer", self.run.run_id)
            user_turn = json.dumps({
                "run_mode": self.run.run_mode,
                "requirements_doc": requirements_doc.model_dump(),
                "current_architecture_md": pkg.state_documents.get("architecture"),
                "tech_stack_md": pkg.state_documents.get("tech_stack"),
                "feature_registry_md": pkg.state_documents.get("feature_registry"),
                "spec_gap_context": spec_gap_context,
            })

            raw = self._invoke_agent(
                "architecture_designer", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
            )

            gate_result = self.gates.evaluate(
                raw=raw,
                expected_model=AgentOutput,
                agent_id="architecture_designer",
                attempt_number=self.run.retry_counters.architecture_validation,
                assembly_id=pkg.assembly_id,
                counters=self.run.retry_counters,
                agent_call_count=self.run.agent_call_count,
                requirements_doc=requirements_doc,
            )

            if not gate_result.layer1_passed:
                outcome = route_malformed(
                    self.run.retry_counters, self._config.to_dict(), "architecture_designer"
                )
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    self._escalate(outcome.escalation_reason or "malformed_output")
                continue

            if not gate_result.layer2_passed:
                outcome = route_p2_invalid(self.run.retry_counters, self._config.to_dict())
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    self._escalate(outcome.escalation_reason or "max_retries_exceeded")
                continue

            if not gate_result.layer3_passed:
                if gate_result.escalation_reason == "block_flag":
                    self._apply_outcome(route_block_flag())
                    self._escalate("block_flag")
                self._apply_outcome(route_p2_lowconf())
                self._escalate("low_confidence")

            data = json.loads(raw)
            output: AgentOutput[Any] = AgentOutput(**data)
            arch_doc = ArchitectureDoc(**output.output)

            # Check for locked tech decisions
            locked = [d for d in arch_doc.tech_decisions if d.locked]
            outcome = route_p2_valid(has_locked_decisions=bool(locked))
            self._apply_outcome(outcome)

            if locked:
                confirmed = human_interface.confirm_tech_decisions(locked)
                if confirmed:
                    tech_data = {"schema_version": "1.0.0", "decisions": [
                        {**d.model_dump(), "run_id": self.run.run_id, "confirmed_at": _now()}
                        for d in locked
                    ]}
                    self.pending.set("tech_stack", tech_data)

            # Store architecture in pending_writes
            self.pending.set("architecture", {
                "schema_version": "1.0.0",
                "last_updated_run": self.run.run_id,
                "modules": [m.model_dump() for m in arch_doc.modules],
                "interfaces": [i.model_dump() for i in arch_doc.interfaces],
                "data_flow": [f.model_dump() for f in arch_doc.data_flow],
            })

            # Store artifact
            ref = self._store_artifact("architecture_doc", "architecture_designer", output)
            self.run.artifacts["architecture_doc"] = ref

            return arch_doc

    # ------------------------------------------------------------------
    # Phase 3 — Implementation (Coder)
    # ------------------------------------------------------------------

    def run_phase3(
        self,
        requirements_doc: RequirementsDoc,
        architecture_doc: ArchitectureDoc,
        system_prompt: str,
        retry_context: dict[str, Any] | None = None,
        code_fix_context: dict[str, Any] | None = None,
    ) -> CodeArtifact:
        """Drive Phase 3 (coding) to completion."""
        from codeforge.schemas.contracts import AgentOutput

        while True:
            pkg = self.assembler.assemble("coder", self.run.run_id)
            user_turn = json.dumps({
                "run_mode": self.run.run_mode,
                "requirements_doc": requirements_doc.model_dump(),
                "architecture_doc": architecture_doc.model_dump(),
                "tech_stack_md": pkg.state_documents.get("tech_stack"),
                "existing_interfaces": [],
                "retry_context": retry_context,
                "code_fix_context": code_fix_context,
            })

            raw = self._invoke_agent(
                "coder", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
            )

            gate_result = self.gates.evaluate(
                raw=raw,
                expected_model=AgentOutput,
                agent_id="coder",
                attempt_number=self.run.retry_counters.coder_validation,
                assembly_id=pkg.assembly_id,
                counters=self.run.retry_counters,
                agent_call_count=self.run.agent_call_count,
                requirements_doc=requirements_doc,
            )

            if not gate_result.layer1_passed:
                outcome = route_malformed(self.run.retry_counters, self._config.to_dict(), "coder")
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    self._escalate(outcome.escalation_reason or "malformed_output")
                continue

            if not gate_result.layer2_passed:
                violation = gate_result.violation_reprompt
                if violation and violation.rule == "requirements_txt_present":
                    outcome = route_p3_no_requirements_txt(self.run.retry_counters, self._config.to_dict())
                else:
                    outcome = route_p3_ac_gap(self.run.retry_counters, self._config.to_dict())
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    self._escalate(outcome.escalation_reason or "max_retries_exceeded")
                continue

            if not gate_result.layer3_passed:
                if gate_result.escalation_reason == "block_flag":
                    self._apply_outcome(route_block_flag())
                    self._escalate("block_flag")
                self._apply_outcome(route_low_confidence("coder"))
                self._escalate("low_confidence")

            data = json.loads(raw)
            output: AgentOutput[Any] = AgentOutput(**data)
            code_artifact = CodeArtifact(**output.output)

            outcome = route_p3_valid()
            self._apply_outcome(outcome)

            ref = self._store_artifact("code_artifact", "coder", output)
            self.run.artifacts["code_artifact"] = ref
            return code_artifact

    # ------------------------------------------------------------------
    # Phase 4A — Code review (Loop A)
    # ------------------------------------------------------------------

    def run_phase4a(
        self,
        requirements_doc: RequirementsDoc,
        architecture_doc: ArchitectureDoc,
        code_artifact: CodeArtifact,
        system_prompt: str,
    ) -> ReviewReport:
        """Drive Loop A (code review) to completion. Returns passing ReviewReport."""
        from codeforge.schemas.contracts import AgentOutput

        while True:
            pkg = self.assembler.assemble("code_reviewer", self.run.run_id)
            user_turn = json.dumps({
                "requirements_doc": requirements_doc.model_dump(),
                "architecture_doc": architecture_doc.model_dump(),
                "decisions_log_md": pkg.state_documents.get("decisions_log", ""),
                "code_artifact": code_artifact.model_dump(),
            })

            raw = self._invoke_agent(
                "code_reviewer", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
            )

            gate_result = self.gates.evaluate(
                raw=raw,
                expected_model=AgentOutput,
                agent_id="code_reviewer",
                attempt_number=self.run.retry_counters.malformed_output,
                assembly_id=pkg.assembly_id,
                counters=self.run.retry_counters,
                agent_call_count=self.run.agent_call_count,
            )

            if not gate_result.layer1_passed or not gate_result.layer2_passed:
                outcome = route_malformed(self.run.retry_counters, self._config.to_dict(), "code_reviewer")
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    self._escalate(outcome.escalation_reason or "malformed_output")
                continue

            if not gate_result.layer3_passed:
                if gate_result.escalation_reason == "block_flag":
                    self._apply_outcome(route_block_flag())
                    self._escalate("block_flag")
                self._apply_outcome(route_low_confidence("code_reviewer"))
                self._escalate("low_confidence")

            data = json.loads(raw)
            output: AgentOutput[Any] = AgentOutput(**data)
            report = ReviewReport(**output.output)

            if report.verdict == "fail":
                outcome = route_p4a_fail(self.run.retry_counters, self._config.to_dict())
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

            outcome = route_p4a_pass(report.verdict == "pass_with_notes")
            self._apply_outcome(outcome)
            ref = self._store_artifact("review_report", "code_reviewer", output)
            self.run.artifacts["review_report"] = ref
            return report

    # ------------------------------------------------------------------
    # Phase 4B — Security review (Loop B)
    # ------------------------------------------------------------------

    def run_phase4b(
        self,
        requirements_doc: RequirementsDoc,
        code_artifact: CodeArtifact,
        system_prompt: str,
    ) -> SecurityReport:
        """Drive Loop B (security review) to completion. Returns passing SecurityReport."""
        from codeforge.schemas.contracts import AgentOutput

        while True:
            pkg = self.assembler.assemble("security_reviewer", self.run.run_id)
            user_turn = json.dumps({
                "tech_stack_md": pkg.state_documents.get("tech_stack", ""),
                "requirements_doc": requirements_doc.model_dump(),
                "code_artifact": code_artifact.model_dump(),
            })

            raw = self._invoke_agent(
                "security_reviewer", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
            )

            gate_result = self.gates.evaluate(
                raw=raw,
                expected_model=AgentOutput,
                agent_id="security_reviewer",
                attempt_number=self.run.retry_counters.malformed_output,
                assembly_id=pkg.assembly_id,
                counters=self.run.retry_counters,
                agent_call_count=self.run.agent_call_count,
            )

            if not gate_result.layer1_passed or not gate_result.layer2_passed:
                outcome = route_malformed(self.run.retry_counters, self._config.to_dict(), "security_reviewer")
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    self._escalate(outcome.escalation_reason or "malformed_output")
                continue

            if not gate_result.layer3_passed:
                if gate_result.escalation_reason == "block_flag":
                    self._apply_outcome(route_block_flag())
                    self._escalate("block_flag")
                self._apply_outcome(route_low_confidence("security_reviewer"))
                self._escalate("low_confidence")

            data = json.loads(raw)
            output: AgentOutput[Any] = AgentOutput(**data)
            report = SecurityReport(**output.output)

            if report.verdict == "fail":
                outcome = route_p4b_fail(self.run.retry_counters, self._config.to_dict())
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

            outcome = route_p4b_pass(report.verdict == "pass_with_notes")
            self._apply_outcome(outcome)
            ref = self._store_artifact("security_report", "security_reviewer", output)
            self.run.artifacts["security_report"] = ref
            return report

    # ------------------------------------------------------------------
    # Phase 5 — Test design
    # ------------------------------------------------------------------

    def run_phase5_design(
        self,
        requirements_doc: RequirementsDoc,
        architecture_doc: ArchitectureDoc,
        system_prompt: str,
        code_fix_context: dict[str, Any] | None = None,
    ) -> TestSuite:
        """Drive test design to completion."""
        from codeforge.schemas.contracts import AgentOutput, InterfaceManifest, DataContract

        while True:
            pkg = self.assembler.assemble("test_designer", self.run.run_id)

            # Build interface_manifest projection
            interface_manifest = {
                "interfaces": [i.model_dump() for i in architecture_doc.interfaces],
                "data_contracts": [dc.model_dump() for dc in requirements_doc.data_contracts],
                "acceptance_criteria": [ac.model_dump() for ac in requirements_doc.acceptance_criteria],
            }

            user_turn = json.dumps({
                "requirements_doc": requirements_doc.model_dump(),
                "interface_manifest": interface_manifest,
                "test_coverage_map_md": pkg.state_documents.get("test_coverage_map", ""),
                "feature_registry_md": pkg.state_documents.get("feature_registry", ""),
                "code_fix_context": code_fix_context,
            })

            raw = self._invoke_agent(
                "test_designer", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
            )

            gate_result = self.gates.evaluate(
                raw=raw,
                expected_model=AgentOutput,
                agent_id="test_designer",
                attempt_number=self.run.retry_counters.test_loop,
                assembly_id=pkg.assembly_id,
                counters=self.run.retry_counters,
                agent_call_count=self.run.agent_call_count,
                requirements_doc=requirements_doc,
            )

            if not gate_result.layer1_passed:
                outcome = route_malformed(self.run.retry_counters, self._config.to_dict(), "test_designer")
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    self._escalate(outcome.escalation_reason or "malformed_output")
                continue

            if not gate_result.layer2_passed:
                outcome = route_p5d_covmap_invalid(self.run.retry_counters, self._config.to_dict())
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    self._escalate(outcome.escalation_reason or "max_retries_exceeded")
                continue

            if not gate_result.layer3_passed:
                if gate_result.escalation_reason == "block_flag":
                    self._apply_outcome(route_block_flag())
                    self._escalate("block_flag")
                self._apply_outcome(route_low_confidence("test_designer"))
                self._escalate("low_confidence")

            data = json.loads(raw)
            output: AgentOutput[Any] = AgentOutput(**data)
            test_suite = TestSuite(**output.output)

            ref = self._store_artifact("test_suite", "test_designer", output)
            self.run.artifacts["test_suite"] = ref
            return test_suite

    # ------------------------------------------------------------------
    # Phase 5 — Test analysis
    # ------------------------------------------------------------------

    def run_phase5_analysis(
        self,
        requirements_doc: RequirementsDoc,
        test_suite: TestSuite,
        test_runner_results: dict[str, Any],
        system_prompt: str,
    ) -> TestAnalysis:
        """Drive test analysis to completion."""
        from codeforge.schemas.contracts import AgentOutput

        while True:
            pkg = self.assembler.assemble("test_analyst", self.run.run_id)
            user_turn = json.dumps({
                "requirements_doc": requirements_doc.model_dump(),
                "test_suite": test_suite.model_dump(),
                "test_runner_results": test_runner_results,
                "test_coverage_map_md": pkg.state_documents.get("test_coverage_map", ""),
            })

            raw = self._invoke_agent(
                "test_analyst", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
            )

            gate_result = self.gates.evaluate(
                raw=raw,
                expected_model=AgentOutput,
                agent_id="test_analyst",
                attempt_number=self.run.retry_counters.malformed_output,
                assembly_id=pkg.assembly_id,
                counters=self.run.retry_counters,
                agent_call_count=self.run.agent_call_count,
            )

            if not gate_result.layer1_passed or not gate_result.layer2_passed:
                outcome = route_malformed(self.run.retry_counters, self._config.to_dict(), "test_analyst")
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    self._escalate(outcome.escalation_reason or "malformed_output")
                continue

            if not gate_result.layer3_passed:
                if gate_result.escalation_reason == "block_flag":
                    self._apply_outcome(route_block_flag())
                    self._escalate("block_flag")
                self._apply_outcome(route_low_confidence("test_analyst"))
                self._escalate("low_confidence")

            data = json.loads(raw)
            output: AgentOutput[Any] = AgentOutput(**data)
            analysis = TestAnalysis(**output.output)

            ref = self._store_artifact("test_analysis", "test_analyst", output)
            self.run.artifacts["test_analysis"] = ref
            return analysis

    # ------------------------------------------------------------------
    # Phase 6 — Commit (flush + CommitWriter)
    # ------------------------------------------------------------------

    def run_phase6(self) -> None:
        """Flush pending_writes to disk. CommitWriter invocation handled by CLI layer."""
        counters = self._counters_snap()
        written = flush_pending_writes(
            self.pending,
            self._project_state,
            self.event_log,
            counters,
        )
        self.run.status = "succeeded"
        self.event_log.update_run_snapshot(self.run)

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
