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
    CodeforgeRun,
    CodeforgeStatus,
    CountersSnapshot,
    EscalationEvent,
    EscalationReason,
    HandoffInvocationType,
    LogActor,
    RePromptContext,
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
    """Raised when codeforge must escalate to a human."""

    def __init__(self, reason: EscalationReason, context: str = "") -> None:
        self.reason = reason
        self.context = context
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

        # Stores
        self._project_state = ProjectStateStore(project_dir)
        self._artifact_store = ArtifactStore(run_log_dir)

        # Core components (initialised in start_run)
        self._run: CodeforgeRun | None = None
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
    ) -> CodeforgeRun:
        """
        Initialise a new CodeforgeRun and all components.
        Returns the run object — caller can inspect status after execute().
        """
        run_id = _new_run_id()
        run_log_dir = self._run_log_dir / run_id
        run_log_dir.mkdir(parents=True, exist_ok=True)

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
        self._assembler = ContextAssembler(
            manifest=manifest,
            artifact_store=self._artifact_store,
            project_state=self._project_state,
            pending_writes=self._pending,
            run_log_dir=run_log_dir,
        )

        self._event_log.update_run_snapshot(self._run)
        return self._run

    def resume_run(self, run: CodeforgeRun) -> None:
        """Restore state from a persisted CodeforgeRun (codeforge resume command)."""
        self._run = run
        run_log_dir = self._run_log_dir / run.run_id
        self._pending = PendingWrites(self._project_state)
        self._event_log = EventLog(run_log_dir, run.run_id, self._config.name)
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

        # LLM call
        result = self.router.complete(
            agent_id=typed_agent_id,
            system_prompt=system_prompt,
            user_turn=user_turn,
            run_id=self.run.run_id,
        )

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
        from codeforge.agents.requirements_analyst import RequirementsAnalystAgent
        from codeforge.schemas.contracts import AgentOutput

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

            if not gate_result.layer1_passed:
                reprompt = gate_result.malformed_reprompt
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
                reprompt = None  # fresh round after human input
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
                ref = self._store_artifact("requirements_doc", "requirements_analyst", output)
                self.run.artifacts["requirements_doc"] = ref
                outcome = route_p1_confirmed()
                self._apply_outcome(outcome)
                return RequirementsDoc(**req_doc_data)
            else:
                feedback = human_interface.get_rejection_feedback()
                confirm_rejection = {
                    "rejected_doc_ref": "pending",
                    "rejection_feedback": feedback,
                }
                reprompt = None  # rejection context is in confirm_rejection field
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
        from codeforge.agents.architecture_designer import ArchitectureDesignerAgent
        from codeforge.schemas.contracts import AgentOutput, InterfaceManifest

        reprompt: RePromptContext | None = None

        while True:
            pkg = self.assembler.assemble("architecture_designer", self.run.run_id)

            # Inject orchestrator-managed fields for build_user_turn()
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
                reprompt = gate_result.malformed_reprompt
                outcome = route_malformed(
                    self.run.retry_counters, self._config.to_dict(), "architecture_designer"
                )
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    self._escalate(outcome.escalation_reason or "malformed_output")
                continue

            if not gate_result.layer2_passed:
                reprompt = gate_result.violation_reprompt
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
        from codeforge.agents.coder import CoderAgent
        from codeforge.schemas.contracts import AgentOutput

        reprompt: RePromptContext | None = None

        while True:
            pkg = self.assembler.assemble("coder", self.run.run_id)

            # Inject orchestrator-managed fields for build_user_turn()
            pkg.state_documents["_run_mode"] = self.run.run_mode
            pkg.state_documents["_existing_interfaces"] = json.dumps([])
            pkg.state_documents["_retry_context"] = json.dumps(retry_context)
            pkg.state_documents["_code_fix_context"] = json.dumps(code_fix_context)

            user_turn = CoderAgent(
                "coder", self.router, self._config
            ).build_user_turn(pkg, reprompt)

            raw = self._invoke_agent(
                "coder", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
                reprompt_reason=reprompt.reason if reprompt is not None else None,
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
                reprompt = gate_result.malformed_reprompt
                outcome = route_malformed(self.run.retry_counters, self._config.to_dict(), "coder")
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    self._escalate(outcome.escalation_reason or "malformed_output")
                continue

            if not gate_result.layer2_passed:
                reprompt = gate_result.violation_reprompt
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
        from codeforge.agents.code_reviewer import CodeReviewerAgent
        from codeforge.schemas.contracts import AgentOutput

        reprompt: RePromptContext | None = None

        while True:
            pkg = self.assembler.assemble("code_reviewer", self.run.run_id)

            user_turn = CodeReviewerAgent(
                "code_reviewer", self.router, self._config
            ).build_user_turn(pkg, reprompt)

            raw = self._invoke_agent(
                "code_reviewer", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
                reprompt_reason=reprompt.reason if reprompt is not None else None,
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
                reprompt = gate_result.malformed_reprompt or gate_result.violation_reprompt
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
        from codeforge.agents.security_reviewer import SecurityReviewerAgent
        from codeforge.schemas.contracts import AgentOutput

        reprompt: RePromptContext | None = None

        while True:
            pkg = self.assembler.assemble("security_reviewer", self.run.run_id)

            user_turn = SecurityReviewerAgent(
                "security_reviewer", self.router, self._config
            ).build_user_turn(pkg, reprompt)

            raw = self._invoke_agent(
                "security_reviewer", system_prompt, user_turn,
                assembly_id=pkg.assembly_id,
                reprompt_reason=reprompt.reason if reprompt is not None else None,
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
                reprompt = gate_result.malformed_reprompt or gate_result.violation_reprompt
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
        retry_context: dict[str, Any] | None = None,
    ) -> TestSuite:
        """Drive test design to completion."""
        from codeforge.agents.test_designer import TestDesignerAgent
        from codeforge.schemas.contracts import AgentOutput

        reprompt: RePromptContext | None = None

        while True:
            # Budget check: test_loop must still have remaining budget
            test_loop_limit = self._config.to_dict().get("retry_limits", {}).get("test_loop", 2)
            if self.run.retry_counters.test_loop >= test_loop_limit:
                outcome = route_p5d_covmap_invalid(self.run.retry_counters, self._config.to_dict())
                self._apply_outcome(outcome)
                self._escalate(outcome.escalation_reason or "max_retries_exceeded")

            pkg = self.assembler.assemble("test_designer", self.run.run_id)

            # Inject orchestrator-managed fields for build_user_turn()
            pkg.state_documents["_code_fix_context"] = json.dumps(code_fix_context)
            pkg.state_documents["_retry_context"] = json.dumps(retry_context)

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
                expected_model=AgentOutput,
                agent_id="test_designer",
                attempt_number=self.run.retry_counters.test_loop,
                assembly_id=pkg.assembly_id,
                counters=self.run.retry_counters,
                agent_call_count=self.run.agent_call_count,
                requirements_doc=requirements_doc,
            )

            if not gate_result.layer1_passed:
                reprompt = gate_result.malformed_reprompt
                outcome = route_malformed(self.run.retry_counters, self._config.to_dict(), "test_designer")
                self._apply_outcome(outcome)
                if outcome.decision == "escalate":
                    self._escalate(outcome.escalation_reason or "malformed_output")
                continue

            if not gate_result.layer2_passed:
                reprompt = gate_result.violation_reprompt
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

            # Emit P5D-valid routing event (was previously missing from event log)
            self._apply_outcome(RoutingOutcome(
                row_id="P5D-valid",
                decision="invoke_agent",
                next_state="test_execution",
            ))

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
        from codeforge.agents.test_analyst import TestAnalystAgent
        from codeforge.schemas.contracts import AgentOutput

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
                expected_model=AgentOutput,
                agent_id="test_analyst",
                attempt_number=self.run.retry_counters.malformed_output,
                assembly_id=pkg.assembly_id,
                counters=self.run.retry_counters,
                agent_call_count=self.run.agent_call_count,
            )

            if not gate_result.layer1_passed or not gate_result.layer2_passed:
                reprompt = gate_result.malformed_reprompt or gate_result.violation_reprompt
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
                    "Run `python -m codeforge.config.prompts.prompt_builder` to build prompts."
                )
            prompts[aid] = p.read_text(encoding="utf-8")
        return prompts

    def execute(
        self, human_brief: str, human_interface: Any
    ) -> "tuple[RequirementsDoc, CodeArtifact, TestSuite]":
        """Drive phases 1–6. CommitWriter is handled by the CLI caller."""
        from codeforge.agents.test_runner import InfrastructureError, TestRunner
        from codeforge.schemas.contracts import TestRunnerInput

        prompts = self._load_prompts()
        req_doc = self.run_phase1(human_brief, human_interface, prompts["requirements_analyst"])

        next_state = "architecture"
        spec_gap_context: dict[str, Any] | None = None
        code_fix_context: dict[str, Any] | None = None
        arch_doc: ArchitectureDoc | None = None
        code_art: CodeArtifact | None = None
        test_suite: TestSuite | None = None
        runner_results: Any = None

        while next_state != "done":
            if next_state == "architecture":
                assert arch_doc is not None or next_state == "architecture"
                arch_doc = self.run_phase2(
                    req_doc, prompts["architecture_designer"], human_interface,
                    spec_gap_context=spec_gap_context,
                )
                spec_gap_context = None
                next_state = "coding"

            elif next_state == "coding":
                assert arch_doc is not None
                code_art = self._run_impl_with_reviews(
                    req_doc, arch_doc, prompts, human_interface, code_fix_context
                )
                next_state = "test_design"

            elif next_state == "test_design":
                assert arch_doc is not None
                test_suite = self.run_phase5_design(
                    req_doc, arch_doc, prompts["test_designer"],
                    code_fix_context=code_fix_context,
                )
                code_fix_context = None  # consumed; clear after test design completes
                next_state = "test_execution"

            elif next_state == "test_execution":
                assert code_art is not None
                assert test_suite is not None
                runner = TestRunner(self._config)
                try:
                    runner_results = runner.run(
                        TestRunnerInput(
                            test_suite=test_suite,
                            code_artifact=code_art,
                            run_config={},
                        )
                    )
                except InfrastructureError as exc:
                    self._escalate("human_required", str(exc))
                next_state = "test_analysis"

            elif next_state == "test_analysis":
                assert test_suite is not None
                assert runner_results is not None
                analysis = self.run_phase5_analysis(
                    req_doc, test_suite, runner_results.model_dump(), prompts["test_analyst"]
                )
                verdict = analysis.verdict

                if verdict == "pass":
                    outcome = route_p5c_pass()
                    self._apply_outcome(outcome)
                    next_state = "done"

                elif verdict == "fail_code_bug":
                    outcome = route_p5c_code_bug(self.run.retry_counters, self._config.to_dict())
                    self._apply_outcome(outcome)
                    if outcome.decision == "escalate":
                        self._escalate(outcome.escalation_reason or "max_retries_exceeded")
                    code_fix_context = _build_code_fix_context(analysis, test_suite)
                    next_state = "coding"

                elif verdict == "fail_test_bug":
                    outcome = route_p5c_test_bug(self.run.retry_counters, self._config.to_dict())
                    self._apply_outcome(outcome)
                    if outcome.decision == "escalate":
                        self._escalate(outcome.escalation_reason or "max_retries_exceeded")
                    next_state = "test_design"

                elif verdict == "fail_spec_gap":
                    outcome = route_p5c_spec_gap(self.run.retry_counters, self._config.to_dict())
                    self._apply_outcome(outcome)
                    if outcome.decision == "escalate":
                        self._escalate(outcome.escalation_reason or "max_retries_exceeded")
                    spec_gap_context = _build_spec_gap_context(analysis)
                    next_state = "architecture"

                elif verdict == "fail_ambiguous":
                    outcome = route_p5c_ambiguous()
                    self._apply_outcome(outcome)
                    self._escalate(outcome.escalation_reason or "human_required")

                else:  # "error"
                    outcome = route_p5c_analyst_error(
                        self.run.retry_counters, self._config.to_dict()
                    )
                    self._apply_outcome(outcome)
                    if outcome.decision == "escalate":
                        self._escalate(outcome.escalation_reason or "human_required")
                    next_state = "test_execution"

        self.run_phase6()
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
    ) -> CodeArtifact:
        """Inner loop: coder → code review → security review. Retries on review failure."""
        retry_context: dict[str, Any] | None = None
        while True:
            code_art = self.run_phase3(
                req_doc, arch_doc, prompts["coder"], retry_context, code_fix_context
            )
            code_fix_context = None  # only applies to the first coding attempt

            try:
                self.run_phase4a(req_doc, arch_doc, code_art, prompts["code_reviewer"])
            except _ReviewFailed as exc:
                retry_context = {
                    "trigger": "code_review_fail",
                    "review_findings": [f.model_dump() for f in exc.report.findings],
                    "review_summary": exc.report.summary,
                }
                continue

            try:
                self.run_phase4b(req_doc, code_art, prompts["security_reviewer"])
            except _ReviewFailed as exc:
                retry_context = {
                    "trigger": "security_review_fail",
                    "security_findings": [f.model_dump() for f in exc.report.findings],
                    "security_summary": exc.report.summary,
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


def _build_spec_gap_context(analysis: "TestAnalysis") -> dict[str, Any]:
    """Build the spec_gap_context dict passed to the architecture designer."""
    result: dict[str, Any] = {
        "trigger": "test_fail_spec_gap",
        "test_summary": analysis.summary,
        "failure_analyses": [fa.model_dump() for fa in analysis.failure_analyses],
    }
    return result
