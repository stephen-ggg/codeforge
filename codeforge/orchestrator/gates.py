"""
orchestrator/gates.py — Gate evaluation for the orchestrator.

Wraps the three-stage validator from schemas/validation.py and adds:
  - Pre-invocation global ceiling check
  - Cross-document contract checks (ac_coverage_must, arch_criteria_coverage,
    coverage_map_valid) that need access to both the output AND the requirements_doc
  - D9 rule: error severity forces verdict: fail (code reviewer)
  - D9 rule: critical severity forces verdict: fail (security reviewer)
  - Logging gate events to the event log

All gate evaluation is deterministic code. No LLM involved.
"""

from __future__ import annotations

from typing import Any

from codeforge.schemas.contracts import (
    AgentId,
    AgentOutput,
    CodeArtifact,
    ContractViolationRePrompt,
    CountersSnapshot,
    EscalationReason,
    GateRule,
    MalformedOutputRePrompt,
    RequirementsDoc,
    RetryCounters,
    ReviewReport,
    SecurityReport,
    ArchitectureDoc,
    TestSuite,
)
from codeforge.schemas.validation import OutputValidator
from codeforge.orchestrator.event_log import EventLog


class GateResult:
    """Result of running all three validation stages against one agent output."""

    def __init__(self) -> None:
        self.structural_passed: bool = True
        self.contract_passed: bool = True
        self.policy_passed: bool = True
        self.malformed_reprompt: MalformedOutputRePrompt | None = None
        self.violation_reprompt: ContractViolationRePrompt | None = None
        self.escalation_reason: EscalationReason | None = None
        self.verdict_forced: bool = False  # D9: error/critical severity forced fail
        # Populated on a terminal policy failure (block_flag / low_confidence) so the
        # state machine can persist the offending output and emit the failing gate
        # event with a real artifact_ref. See GateEvaluator.evaluate().
        self.parsed_output: AgentOutput[Any] | None = None
        self.policy_gate_rule: GateRule | None = None

    @property
    def passed(self) -> bool:
        return self.structural_passed and self.contract_passed and self.policy_passed


class GateEvaluator:
    """
    Runs all three validation layers against agent outputs and logs gate events.

    The orchestrator creates one GateEvaluator per run and calls it after
    each agent invocation.
    """

    def __init__(
        self,
        validator: OutputValidator,
        event_log: EventLog,
        config_snapshot: dict[str, Any],
    ) -> None:
        self._validator = validator
        self._log = event_log
        self._config = config_snapshot

    def check_global_ceiling(
        self,
        agent_call_count: int,
        counters: CountersSnapshot,
    ) -> bool:
        """
        Pre-invocation gate: agent_call_count < max_agent_calls_per_run.
        Returns True if within budget (safe to invoke), False if ceiling exceeded.
        """
        max_calls = self._config.get("global_ceiling", {}).get("max_agent_calls_per_run", 40)
        passed: bool = agent_call_count < max_calls
        self._log.emit_gate(
            rule="global_ceiling",
            passed=passed,
            source_agent="orchestrator",
            counters=counters,
            detail=f"agent_call_count={agent_call_count} max={max_calls}",
        )
        return passed

    def evaluate(
        self,
        raw: str,
        expected_model: type,
        agent_id: AgentId,
        attempt_number: int,
        assembly_id: str,
        counters: RetryCounters,
        agent_call_count: int,
        # Extra context for cross-document contract checks
        requirements_doc: RequirementsDoc | None = None,
    ) -> GateResult:
        """
        Run all three validation stages against raw agent output.

        Structural fires first. Contract and policy only run if structural passes.
        Gate events are emitted for every rule checked.
        """
        result = GateResult()
        counters_snap = self._make_counters_snap(counters, agent_call_count)

        # --- Structural validation ---
        structural_ok, malformed = self._validator.validate_structural(
            raw=raw,
            expected_model=expected_model,
            attempt_number=attempt_number,
            original_input_ref=assembly_id,
        )
        self._log.emit_gate(
            rule="schema_valid",
            passed=structural_ok,
            source_agent=agent_id,
            counters=counters_snap,
            detail="structural validation against Pydantic model" if structural_ok
                   else f"validation errors: {len(malformed.validation_errors) if malformed else 0}",
        )

        if not structural_ok:
            result.structural_passed = False
            result.malformed_reprompt = malformed
            return result

        # Parse the validated output
        import json
        from typing import cast
        data = json.loads(raw)
        output: AgentOutput[Any] = AgentOutput(**data)

        # --- D9: force verdict to fail if error/critical severity present ---
        self._apply_severity_force(output, agent_id)

        # --- Contract validation ---
        contract_ok, violation = self._validator.validate_contract(
            output=output,
            agent_id=agent_id,
            attempt_number=attempt_number,
            original_input_ref=assembly_id,
        )

        # Cross-document contract checks (need requirements_doc)
        if contract_ok and requirements_doc is not None:
            contract_ok, violation = self._cross_document_checks(
                output=output,
                agent_id=agent_id,
                requirements_doc=requirements_doc,
                attempt_number=attempt_number,
                assembly_id=assembly_id,
            )

        from codeforge.schemas.contracts import GateRule as GateRuleType
        if violation and not contract_ok:
            fail_rule: GateRuleType = violation.rule
        else:
            fail_rule = "schema_valid"
        self._log.emit_gate(
            rule=fail_rule,
            passed=contract_ok,
            source_agent=agent_id,
            counters=counters_snap,
            detail="contract rules passed" if contract_ok else (violation.detail if violation else ""),
        )

        if not contract_ok:
            result.contract_passed = False
            result.violation_reprompt = violation
            return result

        # --- Policy validation (confidence, block flags, global ceiling) ---
        policy_ok, escalation_reason = self._validator.validate_policy(
            output=output,
            agent_id=agent_id,
            counters=counters,
        )

        if not policy_ok:
            # Asymmetry: the PASSING policy gate is emitted below, but the FAILING one
            # is emitted by the state machine (_handle_policy_escalation) after it has
            # persisted the offending output — so the gate event can carry a real
            # artifact_ref and a self-sufficient detail (flag reason + summary).
            block_rule: GateRule = "block_flag_present"
            conf_rule: GateRule = "confidence_threshold"
            result.policy_gate_rule = block_rule if escalation_reason == "block_flag" else conf_rule
            result.parsed_output = output
            result.policy_passed = False
            result.escalation_reason = escalation_reason
            return result

        self._log.emit_gate(
            rule="confidence_threshold",
            passed=True,
            source_agent=agent_id,
            counters=counters_snap,
            detail=f"confidence={output.confidence}",
        )

        return result

    def _apply_severity_force(
        self,
        output: AgentOutput[Any],
        agent_id: AgentId,
    ) -> None:
        """
        D9 rule: error severity forces verdict: fail (code reviewer).
                 critical severity forces verdict: fail (security reviewer).
        Mutates the output payload in-place before contract validation runs.
        """
        payload = output.output
        if agent_id == "code_reviewer" and isinstance(payload, ReviewReport):
            if payload.verdict != "fail" and any(
                f.severity == "error" for f in payload.findings
            ):
                object.__setattr__(payload, "verdict", "fail")

        elif agent_id == "security_reviewer" and isinstance(payload, SecurityReport):
            if payload.verdict != "fail" and any(
                f.severity == "critical" for f in payload.findings
            ):
                object.__setattr__(payload, "verdict", "fail")

    def _cross_document_checks(
        self,
        output: AgentOutput[Any],
        agent_id: AgentId,
        requirements_doc: RequirementsDoc,
        attempt_number: int,
        assembly_id: str,
    ) -> tuple[bool, ContractViolationRePrompt | None]:
        """
        Contract checks that require both the agent output AND the requirements_doc.
        These can't be done in the validator alone since it only sees one document.
        """
        payload = output.output

        # ac_coverage_must: coder must declare all must-priority ACs covered
        if agent_id == "coder" and isinstance(payload, CodeArtifact):
            must_ids = [
                ac.id for ac in requirements_doc.acceptance_criteria
                if ac.priority == "must"
            ]
            uncovered = self._validator.check_ac_coverage_must(
                payload.criteria_addressed, must_ids
            )
            if uncovered:
                return False, self._validator._make_violation(
                    rule="ac_coverage_must",
                    detail=f"must-priority ACs not declared covered: {uncovered}",
                    uncovered_ac_ids=uncovered,
                    attempt_number=attempt_number,
                    original_input_ref=assembly_id,
                    counter="coder_validation",
                )

        # arch_criteria_coverage: all must ACs must appear in criteria_coverage with ≥1 valid module
        elif agent_id == "architecture_designer" and isinstance(payload, ArchitectureDoc):
            must_ids = [
                ac.id for ac in requirements_doc.acceptance_criteria
                if ac.priority == "must"
            ]
            valid_modules = {m.name for m in payload.modules}
            unaddressed = self._validator.check_arch_criteria_coverage(
                payload.criteria_coverage, must_ids, valid_modules
            )
            if unaddressed:
                return False, self._validator._make_violation(
                    rule="arch_criteria_coverage",
                    detail=f"must-priority ACs not in criteria_coverage: {unaddressed}",
                    unaddressed_ac_ids=unaddressed,
                    attempt_number=attempt_number,
                    original_input_ref=assembly_id,
                    counter="architecture_validation",
                )

        # coverage_map_valid: test designer coverage_map AC ids must match requirements_doc
        elif agent_id == "test_designer" and isinstance(payload, TestSuite):
            valid_ids = {ac.id for ac in requirements_doc.acceptance_criteria}
            coverage_map_list: list[dict[str, Any]] = payload.coverage_map
            mismatched = self._validator.check_coverage_map_valid(coverage_map_list, valid_ids)
            if mismatched:
                return False, self._validator._make_violation(
                    rule="coverage_map_valid",
                    detail=f"coverage_map contains AC ids not in requirements_doc: {mismatched}",
                    mismatched_criterion_ids=mismatched,
                    attempt_number=attempt_number,
                    original_input_ref=assembly_id,
                    counter="test_loop",
                )

        return True, None

    def _make_counters_snap(
        self,
        counters: RetryCounters,
        agent_call_count: int,
    ) -> CountersSnapshot:
        return CountersSnapshot(
            **counters.model_dump(),
            agent_call_count=agent_call_count,
        )
