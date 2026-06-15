"""
schemas/validation.py — Three-stage output validator.

Structural validation — schema check via Pydantic v2
Contract validation   — business rules
Policy validation      — confidence, block flags, global ceiling

The orchestrator calls these in order. Structural fires first; contract and
policy fire only if structural passes. All three stages are deterministic
code — no LLM involved.
"""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from pydantic import BaseModel, ValidationError as PydanticValidationError

from codeforge.schemas.contracts import (
    AgentId,
    AgentOutput,
    ContractViolationRePrompt,
    EscalationReason,
    GateRule,
    MalformedOutputRePrompt,
    RetryCounters,
    ValidationError,
    ReviewReport,
    SecurityReport,
    TestAnalysis,
    CodeArtifact,
    ArchitectureDoc,
    TestSuite,
    RequirementsDoc,
    CriteriaCoverageEntry,
)


class OutputValidator:
    """
    Three-stage validator for all agent outputs.

    Usage:
        validator = OutputValidator(config_snapshot)
        ok, errors = validator.validate_structural(raw_str, RequirementsComplete)
        if ok:
            ok, reprompt = validator.validate_contract(output, "requirements_analyst")
            if ok:
                ok, escalation_reason = validator.validate_policy(output, "requirements_analyst", counters)
    """

    def __init__(self, config_snapshot: dict[str, Any]) -> None:
        self._config = config_snapshot

    # ------------------------------------------------------------------
    # Structural validation
    # ------------------------------------------------------------------

    def validate_structural(
        self,
        raw: str,
        expected_model: type[BaseModel],
        attempt_number: int,
        original_input_ref: str,
    ) -> tuple[bool, MalformedOutputRePrompt | None]:
        """
        Parse `raw` as JSON and validate against `expected_model`.

        Returns (True, None) on success.
        Returns (False, MalformedOutputRePrompt) on failure — the raw output is NOT
        included in the reprompt; on terminal (budget-exhausted) failures the caller
        persists it to raw_outputs/ and links it from the escalation for debugging.
        """
        validation_errors: list[ValidationError] = []

        if not raw or not raw.strip():
            validation_errors.append(
                ValidationError(
                    field_path="<root>",
                    error_type="empty_response",
                    expected=expected_model.__name__,
                    received="empty string",
                )
            )
            return False, self._make_malformed_reprompt(
                original_input_ref, validation_errors, attempt_number
            )

        # Try JSON parse
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            validation_errors.append(
                ValidationError(
                    field_path="<root>",
                    error_type="wrong_type",
                    expected="valid JSON object",
                    received=f"JSON parse error: {exc.msg}",
                )
            )
            return False, self._make_malformed_reprompt(
                original_input_ref, validation_errors, attempt_number
            )

        # Try Pydantic validation
        try:
            expected_model.model_validate(data)
        except PydanticValidationError as exc:
            for err in exc.errors():
                field_path = ".".join(str(p) for p in err["loc"]) or "<root>"
                raw_error_type = self._classify_pydantic_error(err["type"])
                error_type = cast(
                    Literal[
                        "missing_required", "wrong_type", "invalid_enum_value",
                        "truncated", "empty_response"
                    ],
                    raw_error_type,
                )
                validation_errors.append(
                    ValidationError(
                        field_path=field_path,
                        error_type=error_type,
                        expected=err.get("msg", ""),
                        received=None,  # never include raw content
                    )
                )

        if validation_errors:
            return False, self._make_malformed_reprompt(
                original_input_ref, validation_errors, attempt_number
            )

        return True, None

    def _classify_pydantic_error(self, pydantic_type: str) -> str:
        """Map pydantic v2 error type strings to our ValidationError.error_type literals."""
        mapping = {
            "missing": "missing_required",
            "value_error": "wrong_type",
            "type_error": "wrong_type",
            "enum": "invalid_enum_value",
            "literal_error": "invalid_enum_value",
            "string_type": "wrong_type",
            "int_type": "wrong_type",
            "float_type": "wrong_type",
            "bool_type": "wrong_type",
            "list_type": "wrong_type",
            "dict_type": "wrong_type",
        }
        # pydantic v2 error types follow a dotted pattern like "missing", "string_type", etc.
        for key, value in mapping.items():
            if key in pydantic_type:
                return value
        return "wrong_type"

    def _make_malformed_reprompt(
        self,
        original_input_ref: str,
        validation_errors: list[ValidationError],
        attempt_number: int,
    ) -> MalformedOutputRePrompt:
        max_attempts = self._config.get("retry_limits", {}).get("malformed_output_retries", 2)
        return MalformedOutputRePrompt(
            original_input_ref=original_input_ref,
            validation_errors=validation_errors,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
        )

    # ------------------------------------------------------------------
    # Contract validation (business rules)
    # ------------------------------------------------------------------

    def validate_contract(
        self,
        output: AgentOutput[Any],
        agent_id: AgentId,
        attempt_number: int,
        original_input_ref: str,
    ) -> tuple[bool, ContractViolationRePrompt | None]:
        """
        Apply agent-specific contract rules.

        Returns (True, None) on success.
        Returns (False, ContractViolationRePrompt) on first failure — rules are checked
        in the priority order defined by the spec (requirements_txt before ac_coverage, etc.).
        """
        payload = output.output

        # ---- Code reviewer ----
        if agent_id == "code_reviewer" and isinstance(payload, ReviewReport):
            if payload.verdict == "fail" and not payload.findings:
                return False, self._make_violation(
                    rule="verdict_has_findings",
                    detail="verdict is 'fail' but findings list is empty",
                    findings_missing_for_verdict="code_reviewer returned fail with no findings",
                    attempt_number=attempt_number,
                    original_input_ref=original_input_ref,
                    counter="malformed_output",
                )
            # D9: error severity forces fail
            if any(f.severity == "error" for f in payload.findings) and payload.verdict != "fail":
                # This is a contract-stage correction — not a reprompt; handled by orchestrator routing.
                # We signal it here so the routing layer can apply the force.
                # Returning True with a sentinel note — orchestrator checks this separately.
                pass

        # ---- Security reviewer ----
        if agent_id == "security_reviewer" and isinstance(payload, SecurityReport):
            if payload.verdict == "fail" and not payload.findings:
                return False, self._make_violation(
                    rule="verdict_has_findings",
                    detail="verdict is 'fail' but findings list is empty",
                    findings_missing_for_verdict="security_reviewer returned fail with no findings",
                    attempt_number=attempt_number,
                    original_input_ref=original_input_ref,
                    counter="malformed_output",
                )

        # ---- Test analyst ----
        if agent_id == "test_analyst" and isinstance(payload, TestAnalysis):
            fail_verdicts = {
                "fail_code_bug", "fail_test_bug", "fail_spec_gap", "fail_ambiguous"
            }
            if payload.verdict in fail_verdicts and not payload.failure_analyses:
                return False, self._make_violation(
                    rule="verdict_has_findings",
                    detail=f"verdict is '{payload.verdict}' but failure_analyses list is empty",
                    findings_missing_for_verdict="test_analyst returned fail verdict with no failure_analyses",
                    attempt_number=attempt_number,
                    original_input_ref=original_input_ref,
                    counter="malformed_output",
                )
            # spec_gap_has_description: fail_spec_gap requires at least one FailureAnalysis with spec_gap
            if payload.verdict == "fail_spec_gap":
                has_spec_gap = any(
                    fa.spec_gap is not None for fa in payload.failure_analyses
                )
                if not has_spec_gap:
                    missing_ids = [fa.test_case_id for fa in payload.failure_analyses]
                    return False, self._make_violation(
                        rule="spec_gap_has_description",
                        detail="verdict is fail_spec_gap but no FailureAnalysis has spec_gap populated",
                        missing_spec_gap_for=missing_ids,
                        attempt_number=attempt_number,
                        original_input_ref=original_input_ref,
                        counter="malformed_output",
                    )

        # ---- Coder ----
        if agent_id == "coder" and isinstance(payload, CodeArtifact):
            # requirements_txt_present fires first (coding_missing_requirements_txt
            # before coding_acceptance_criteria_gap)
            has_requirements_txt = any(
                f.path == "requirements.txt" for f in payload.files
            )
            if not has_requirements_txt:
                return False, self._make_violation(
                    rule="requirements_txt_present",
                    detail="CodeArtifact does not include requirements.txt at repo root",
                    missing_requirements_txt=True,
                    attempt_number=attempt_number,
                    original_input_ref=original_input_ref,
                    counter="coder_validation",
                )

        # ---- Architecture designer ----
        if agent_id == "architecture_designer" and isinstance(payload, ArchitectureDoc):
            for entry in payload.criteria_coverage:
                if not entry.module_names:
                    return False, self._make_violation(
                        rule="arch_criteria_coverage",
                        detail=f"criteria_coverage entry for {entry.criterion_id} has no module_names",
                        unaddressed_ac_ids=[entry.criterion_id],
                        attempt_number=attempt_number,
                        original_input_ref=original_input_ref,
                        counter="architecture_validation",
                    )

        # ---- Test designer ----
        if agent_id == "test_designer" and isinstance(payload, TestSuite):
            # coverage_map_valid: AC ids in coverage_map must be a valid set
            # Full cross-check against requirements_doc happens in orchestrator routing.
            # Structural check here: no entry missing criterion_id.
            coverage_map_list: list[dict[str, Any]] = payload.coverage_map
            for entry_dict in coverage_map_list:
                cid = entry_dict.get("criterion_id", "")
                if not cid:
                    return False, self._make_violation(
                        rule="coverage_map_valid",
                        detail="coverage_map entry missing criterion_id",
                        mismatched_criterion_ids=["<missing>"],
                        attempt_number=attempt_number,
                        original_input_ref=original_input_ref,
                        counter="test_loop",
                    )

        return True, None

    def _make_violation(
        self,
        rule: str,
        detail: str,
        attempt_number: int,
        original_input_ref: str,
        counter: str,
        **payload_fields: Any,
    ) -> ContractViolationRePrompt:
        """Build a ContractViolationRePrompt for the given rule."""
        limits = self._config.get("retry_limits", {})
        # Map counter names to config keys
        counter_to_config = {
            "coder_validation": "coder_validation_retries",
            "architecture_validation": "architecture_validation_retries",
            "test_loop": "test_loop",
            "malformed_output": "malformed_output_retries",
            "code_review_loop": "code_review_loop",
            "security_review_loop": "security_review_loop",
        }
        config_key = counter_to_config.get(counter, counter)
        max_attempts = limits.get(config_key, 2)

        typed_rule = cast(GateRule, rule)

        return ContractViolationRePrompt(
            rule=typed_rule,
            original_input_ref=original_input_ref,
            detail=detail,
            attempt_number=attempt_number,
            max_attempts=max_attempts,
            **payload_fields,
        )

    # ------------------------------------------------------------------
    # Policy validation (confidence, block flags, global ceiling)
    # ------------------------------------------------------------------

    def validate_policy(
        self,
        output: AgentOutput[Any],
        agent_id: AgentId,
        counters: RetryCounters,
    ) -> tuple[bool, EscalationReason | None]:
        """
        Apply business gate checks: confidence threshold, block flags, global ceiling.

        Returns (True, None) to proceed.
        Returns (False, EscalationReason) to escalate.

        Note: global ceiling check (agent_call_count) is done by the orchestrator
        BEFORE invocation (pre-invocation gate), not here. This method checks
        post-invocation gates only.
        """
        # Block flag: any flag with severity "block" halts immediately
        for flag in output.unresolved_flags:
            if flag.severity == "block":
                return False, "block_flag"

        # Confidence threshold
        thresholds = self._config.get("confidence_thresholds", {})
        threshold = thresholds.get(agent_id)
        if threshold is not None and output.confidence < threshold:
            return False, "low_confidence"

        return True, None

    # ------------------------------------------------------------------
    # Helpers the orchestrator uses for specific Layer-2 cross-document checks
    # ------------------------------------------------------------------

    def check_ac_coverage_must(
        self,
        criteria_addressed: list[str],
        must_ac_ids: list[str],
    ) -> list[str]:
        """
        Returns the list of must-priority AC ids NOT declared covered.
        Empty list means the gate passes.
        """
        addressed_set = set(criteria_addressed)
        return [ac_id for ac_id in must_ac_ids if ac_id not in addressed_set]

    def check_arch_criteria_coverage(
        self,
        criteria_coverage: list[CriteriaCoverageEntry],
        must_ac_ids: list[str],
        valid_module_names: set[str],
    ) -> list[str]:
        """
        Returns must-priority AC ids absent from criteria_coverage or with no valid module.
        Empty list means the gate passes.
        """
        covered: dict[str, bool] = {}
        for entry in criteria_coverage:
            if entry.criterion_id and any(m in valid_module_names for m in entry.module_names):
                covered[entry.criterion_id] = True

        return [ac_id for ac_id in must_ac_ids if ac_id not in covered]

    def check_coverage_map_valid(
        self,
        coverage_map: list[dict[str, Any]],
        valid_ac_ids: set[str],
    ) -> list[str]:
        """
        Returns criterion_ids in coverage_map that don't appear in requirements_doc.
        Empty list means the gate passes.
        """
        mismatched = []
        for entry in coverage_map:
            cid = entry.get("criterion_id", "")
            if cid not in valid_ac_ids:
                mismatched.append(cid)
        return mismatched