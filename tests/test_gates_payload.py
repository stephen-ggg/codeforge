from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from codeforge.orchestrator.event_log import EventLog
from codeforge.orchestrator.gates import GateEvaluator
from codeforge.schemas.contracts import (
    AcceptanceCriterion,
    CodeReviewerOutput,
    CoderOutput,
    RequirementsDoc,
    RetryCounters,
    SecurityReviewerOutput,
    TestAnalystOutput,
    TestDesignerOutput,
)
from codeforge.schemas.validation import OutputValidator

_CONFIG: dict[str, Any] = {
    "confidence_thresholds": {"security_reviewer": 0.90},
    "global_ceiling": {"max_agent_calls_per_run": 40},
}

_NEXTJS_CONFIG: dict[str, Any] = {
    "confidence_thresholds": {},
    "global_ceiling": {"max_agent_calls_per_run": 40},
    "stack_profile": {"manifest_filename": "package.json", "manifest_required": True},
}


@pytest.fixture
def gates(tmp_path: Path) -> GateEvaluator:
    validator = OutputValidator(_CONFIG)
    event_log = EventLog(tmp_path / "run", "run-test", "0.0.0")
    return GateEvaluator(validator, event_log, _CONFIG)


@pytest.fixture
def nextjs_gates(tmp_path: Path) -> GateEvaluator:
    validator = OutputValidator(_NEXTJS_CONFIG)
    event_log = EventLog(tmp_path / "run-nx", "run-nx", "0.0.0")
    return GateEvaluator(validator, event_log, _NEXTJS_CONFIG)


def _requirements_doc(*must_ids: str) -> RequirementsDoc:
    """Minimal RequirementsDoc whose listed ACs are all must-priority."""
    return RequirementsDoc(
        run_id="run-1",
        run_mode="new_project",
        feature_title="t",
        feature_description="d",
        scope={"in_scope": [], "explicitly_out_of_scope": []},
        acceptance_criteria=[
            AcceptanceCriterion(id=i, description="d", testable=True, priority="must")
            for i in must_ids
        ],
        data_contracts=[],
        human_confirmed_decisions=[],
    )


_SECURITY_CATEGORIES = (
    "injection", "secrets", "input_validation", "authentication", "authorisation",
    "dependency_vulnerabilities", "sensitive_data_exposure", "xss",
    "insecure_direct_object_references", "error_handling",
)


def _full_checklist() -> list[dict[str, Any]]:
    """One entry per canonical category — satisfies the security_checklist_complete gate."""
    return [
        {"category": cat, "assessed": True, "result": "not_applicable", "notes": "n/a"}
        for cat in _SECURITY_CATEGORIES
    ]


def _security_envelope(finding: dict[str, Any], verdict: str = "pass") -> str:
    return json.dumps({
        "output": {
            "verdict": verdict,
            "summary": "test",
            "findings": [finding],
            "checklist": _full_checklist(),
        },
        "assumptions_made": [],
        "confidence": 0.99,
        "unresolved_flags": [],
    })


def _evaluate(gates: GateEvaluator, raw: str) -> Any:
    return gates.evaluate(
        raw=raw,
        expected_model=SecurityReviewerOutput,
        agent_id="security_reviewer",
        attempt_number=0,
        assembly_id="assembly-1",
        counters=RetryCounters(),
        agent_call_count=1,
    )


def test_malformed_nested_line_range_triggers_reprompt_not_crash(gates: GateEvaluator) -> None:
    """A nested payload error (line_range as [{start,end}] instead of [int,int])
    must surface as a structural failure / re-prompt — never an unhandled crash."""
    finding = {
        "id": "F1", "file": "math.py",
        "line_range": [{"start": 37, "end": 41}],  # wrong shape
        "category": "input_validation", "severity": "warn",
        "description": "x", "recommended_fix": "y",
    }
    result = _evaluate(gates, _security_envelope(finding))
    assert result.structural_passed is False
    assert result.malformed_reprompt is not None


def test_d9_critical_severity_forces_fail_verdict(gates: GateEvaluator) -> None:
    """A typed payload with a critical finding and verdict 'pass' is forced to
    'fail' by D9 — and the gate returns that mutated, typed output."""
    finding = {
        "id": "F1", "file": "math.py",
        "line_range": [37, 41],
        "category": "injection", "severity": "critical",
        "description": "x", "recommended_fix": "y",
    }
    result = _evaluate(gates, _security_envelope(finding, verdict="pass"))
    assert result.passed
    assert result.parsed_output is not None
    assert result.parsed_output.output.verdict == "fail"


def _review_finding(severity: str) -> dict[str, Any]:
    return {
        "id": "RF1", "file": "math.py", "line_range": [37, 41],
        "category": "correctness", "severity": severity,
        "description": "x", "suggested_fix": "y",
    }


def _code_review_envelope(severity: str, verdict: str = "pass") -> str:
    return json.dumps({
        "output": {
            "verdict": verdict, "summary": "s",
            "findings": [_review_finding(severity)],
            "criteria_coverage": [],
        },
        "assumptions_made": [], "confidence": 0.99, "unresolved_flags": [],
    })


def _evaluate_code_reviewer(gates: GateEvaluator, raw: str) -> Any:
    # No requirements_doc -> the review_criteria_coverage cross-doc check is skipped,
    # isolating the D9 severity-force behaviour.
    return gates.evaluate(
        raw=raw, expected_model=CodeReviewerOutput, agent_id="code_reviewer",
        attempt_number=0, assembly_id="a", counters=RetryCounters(), agent_call_count=1,
    )


def test_d9_error_severity_forces_fail_verdict_code_reviewer(gates: GateEvaluator) -> None:
    """A code-review payload with an error-severity finding and verdict 'pass' is
    forced to 'fail' by D9 (the code_reviewer branch of _apply_severity_force)."""
    result = _evaluate_code_reviewer(gates, _code_review_envelope("error", verdict="pass"))
    assert result.parsed_output is not None
    assert result.parsed_output.output.verdict == "fail"


def test_d9_warn_severity_does_not_force_fail_code_reviewer(gates: GateEvaluator) -> None:
    """Negative case: a warn-severity finding must NOT force fail — the verdict the
    reviewer chose stands. Guards against an over-eager force."""
    result = _evaluate_code_reviewer(gates, _code_review_envelope("warn", verdict="pass"))
    assert result.parsed_output is not None
    assert result.parsed_output.output.verdict == "pass"


def test_d9_warn_severity_does_not_force_fail_security_reviewer(gates: GateEvaluator) -> None:
    """Negative case for the security reviewer: a non-critical (warn) finding leaves
    the 'pass' verdict intact."""
    finding = {
        "id": "F1", "file": "math.py", "line_range": [37, 41],
        "category": "input_validation", "severity": "warn",
        "description": "x", "recommended_fix": "y",
    }
    result = _evaluate(gates, _security_envelope(finding, verdict="pass"))
    assert result.parsed_output is not None
    assert result.parsed_output.output.verdict == "pass"


# ---------------------------------------------------------------------------
# block_flag detection — a severity=block unresolved flag halts at the policy gate
# ---------------------------------------------------------------------------

def _security_envelope_with_flag(severity: str) -> str:
    return json.dumps({
        "output": {
            "verdict": "pass", "summary": "test", "findings": [],
            "checklist": _full_checklist(),
        },
        "assumptions_made": [], "confidence": 0.99,
        "unresolved_flags": [{
            "id": "FLAG-001", "description": "blocking issue",
            "severity": severity, "suggested_action": "fix it",
        }],
    })


def test_block_flag_detected_by_policy_gate(gates: GateEvaluator) -> None:
    """A severity=block unresolved flag drives the policy gate to fail with
    escalation_reason=block_flag and rule block_flag_present — exercises the detection
    path (validate_policy), which every state-machine test bypasses by hand-building
    the GateResult."""
    result = _evaluate(gates, _security_envelope_with_flag("block"))
    assert result.policy_passed is False
    assert result.escalation_reason == "block_flag"
    assert result.policy_gate_rule == "block_flag_present"


def test_warn_flag_does_not_trip_block_path(gates: GateEvaluator) -> None:
    """A warn-severity flag is informational — it must NOT trigger the block halt."""
    result = _evaluate(gates, _security_envelope_with_flag("warn"))
    assert result.policy_passed is True
    assert result.escalation_reason is None


def _test_designer_envelope(path_a: str, path_b: str) -> str:
    def _case(cid: str, path: str) -> dict[str, Any]:
        return {
            "id": cid, "title": f"case {cid}", "criterion_ids": ["AC-001"],
            "type": "unit", "description": "d",
            "code": [{
                "path": path, "content": "def test_x():\n    assert True\n",
                "language": "python", "change_type": "new", "change_reason": None,
            }],
            "explicitly_not_testing": [],
        }
    return json.dumps({
        "output": {
            "test_cases": [_case("TC-001", path_a), _case("TC-002", path_b)],
            "test_infrastructure": [],
            "coverage_map": [{"criterion_id": "AC-001", "test_case_ids": ["TC-001", "TC-002"]}],
        },
        "assumptions_made": [], "confidence": 0.9, "unresolved_flags": [],
    })


def _evaluate_test_designer(gates: GateEvaluator, raw: str) -> Any:
    return gates.evaluate(
        raw=raw,
        expected_model=TestDesignerOutput,
        agent_id="test_designer",
        attempt_number=0,
        assembly_id="assembly-1",
        counters=RetryCounters(),
        agent_call_count=1,
    )


def test_duplicate_test_paths_trigger_contract_violation(gates: GateEvaluator) -> None:
    """Two test cases sharing one file path must fail the unique_test_paths contract
    rule — staging overwrites same-path files, so this would silently drop a test."""
    result = _evaluate_test_designer(
        gates, _test_designer_envelope("tests/test_x.py", "tests/test_x.py")
    )
    assert result.contract_passed is False
    assert result.violation_reprompt is not None
    assert result.violation_reprompt.rule == "unique_test_paths"
    assert result.violation_reprompt.duplicate_paths == ["tests/test_x.py"]


def test_unique_test_paths_pass_contract(gates: GateEvaluator) -> None:
    """Distinct file paths (one self-contained file per test case) pass the contract."""
    result = _evaluate_test_designer(
        gates, _test_designer_envelope("tests/test_x.py", "tests/test_y.py")
    )
    assert result.contract_passed is True
    assert result.violation_reprompt is None


# ---------------------------------------------------------------------------
# coverage_map_valid — must-AC completeness (the other direction)
# ---------------------------------------------------------------------------

def _td_envelope_for_acs(covered_ac_ids: list[str]) -> str:
    """One test case per covered AC; coverage_map covers exactly covered_ac_ids."""
    cases = [
        {
            "id": f"TC-{i:03d}", "title": "c", "criterion_ids": [ac],
            "type": "unit", "description": "d",
            "code": [{
                "path": f"tests/test_{i}.py", "content": "def test_x():\n    assert True\n",
                "language": "python", "change_type": "new", "change_reason": None,
            }],
            "explicitly_not_testing": [],
        }
        for i, ac in enumerate(covered_ac_ids)
    ]
    return json.dumps({
        "output": {
            "test_cases": cases,
            "test_infrastructure": [],
            "coverage_map": [
                {"criterion_id": ac, "test_case_ids": [f"TC-{i:03d}"]}
                for i, ac in enumerate(covered_ac_ids)
            ],
        },
        "assumptions_made": [], "confidence": 0.9, "unresolved_flags": [],
    })


def test_coverage_map_uncovered_must_ac_fails(gates: GateEvaluator) -> None:
    """A must AC with no covering test case fails coverage_map_valid (completeness)."""
    result = gates.evaluate(
        raw=_td_envelope_for_acs(["AC-001"]),
        expected_model=TestDesignerOutput,
        agent_id="test_designer",
        attempt_number=0,
        assembly_id="a",
        counters=RetryCounters(),
        agent_call_count=1,
        requirements_doc=_requirements_doc("AC-001", "AC-002"),
    )
    assert result.contract_passed is False
    assert result.violation_reprompt is not None
    assert result.violation_reprompt.rule == "coverage_map_valid"
    assert result.violation_reprompt.uncovered_ac_ids == ["AC-002"]


def test_coverage_map_all_must_covered_passes(gates: GateEvaluator) -> None:
    result = gates.evaluate(
        raw=_td_envelope_for_acs(["AC-001", "AC-002"]),
        expected_model=TestDesignerOutput,
        agent_id="test_designer",
        attempt_number=0,
        assembly_id="a",
        counters=RetryCounters(),
        agent_call_count=1,
        requirements_doc=_requirements_doc("AC-001", "AC-002"),
    )
    assert result.contract_passed is True


# ---------------------------------------------------------------------------
# package_json_dev_script — value must run `next dev`, not just key presence
# ---------------------------------------------------------------------------

def _coder_envelope(dev_value: str | None) -> str:
    scripts = {} if dev_value is None else {"dev": dev_value}
    pkg = {"name": "app", "scripts": scripts}
    return json.dumps({
        "output": {
            "files": [{
                "path": "package.json", "content": json.dumps(pkg),
                "language": "json", "change_type": "new", "change_reason": None,
            }],
            "module_interfaces": {"files": []},
            "change_summary": "s",
            "criteria_addressed": [],
            "interface_changes": [],
        },
        "assumptions_made": [], "confidence": 0.9, "unresolved_flags": [],
    })


def _evaluate_coder(gates: GateEvaluator, raw: str) -> Any:
    return gates.evaluate(
        raw=raw, expected_model=CoderOutput, agent_id="coder",
        attempt_number=0, assembly_id="a", counters=RetryCounters(), agent_call_count=1,
    )


def test_dev_script_wrong_value_fails(nextjs_gates: GateEvaluator) -> None:
    """A `dev` script that doesn't run `next dev` fails — presence alone isn't enough."""
    result = _evaluate_coder(nextjs_gates, _coder_envelope("next build"))
    assert result.contract_passed is False
    assert result.violation_reprompt.rule == "package_json_dev_script"


def test_dev_script_missing_key_fails(nextjs_gates: GateEvaluator) -> None:
    result = _evaluate_coder(nextjs_gates, _coder_envelope(None))
    assert result.contract_passed is False
    assert result.violation_reprompt.rule == "package_json_dev_script"


def test_dev_script_next_dev_with_flags_passes(nextjs_gates: GateEvaluator) -> None:
    result = _evaluate_coder(nextjs_gates, _coder_envelope("next dev --port 3000"))
    assert result.contract_passed is True


# ---------------------------------------------------------------------------
# requirements_txt_present — the default (python) stack requires its manifest
# ---------------------------------------------------------------------------

def _coder_files_envelope(paths: list[str]) -> str:
    files = [
        {"path": p, "content": "x", "language": "text",
         "change_type": "new", "change_reason": None}
        for p in paths
    ]
    return json.dumps({
        "output": {
            "files": files,
            "module_interfaces": {"files": []},
            "change_summary": "s",
            "criteria_addressed": [],
            "interface_changes": [],
        },
        "assumptions_made": [], "confidence": 0.9, "unresolved_flags": [],
    })


def test_requirements_txt_missing_fails(gates: GateEvaluator) -> None:
    """The default stack (manifest_required, requirements.txt) rejects a CodeArtifact
    that omits the manifest at repo root."""
    result = _evaluate_coder(gates, _coder_files_envelope(["app.py"]))
    assert result.contract_passed is False
    assert result.violation_reprompt.rule == "requirements_txt_present"


def test_requirements_txt_present_passes(gates: GateEvaluator) -> None:
    result = _evaluate_coder(gates, _coder_files_envelope(["app.py", "requirements.txt"]))
    assert result.contract_passed is True


# ---------------------------------------------------------------------------
# review_criteria_coverage — reviewer must record every must AC
# ---------------------------------------------------------------------------

def _review_envelope(recorded_ac_ids: list[str]) -> str:
    return json.dumps({
        "output": {
            "verdict": "pass", "summary": "s", "findings": [],
            "criteria_coverage": [
                {"criterion_id": ac, "addressed": True, "notes": "n"}
                for ac in recorded_ac_ids
            ],
        },
        "assumptions_made": [], "confidence": 0.99, "unresolved_flags": [],
    })


def _evaluate_reviewer(gates: GateEvaluator, raw: str, req: RequirementsDoc) -> Any:
    return gates.evaluate(
        raw=raw, expected_model=CodeReviewerOutput, agent_id="code_reviewer",
        attempt_number=0, assembly_id="a", counters=RetryCounters(), agent_call_count=1,
        requirements_doc=req,
    )


def test_review_criteria_coverage_omitted_must_ac_fails(gates: GateEvaluator) -> None:
    result = _evaluate_reviewer(
        gates, _review_envelope(["AC-001"]), _requirements_doc("AC-001", "AC-002")
    )
    assert result.contract_passed is False
    assert result.violation_reprompt.rule == "review_criteria_coverage"
    assert result.violation_reprompt.unrecorded_criterion_ids == ["AC-002"]


def test_review_criteria_coverage_all_recorded_passes(gates: GateEvaluator) -> None:
    result = _evaluate_reviewer(
        gates, _review_envelope(["AC-001", "AC-002"]), _requirements_doc("AC-001", "AC-002")
    )
    assert result.contract_passed is True


# ---------------------------------------------------------------------------
# security_checklist_complete — all ten categories assessed
# ---------------------------------------------------------------------------

def test_security_checklist_incomplete_fails(gates: GateEvaluator) -> None:
    raw = json.dumps({
        "output": {
            "verdict": "pass", "summary": "s", "findings": [],
            "checklist": [
                {"category": "injection", "assessed": True, "result": "clean", "notes": "n"}
            ],  # only 1 of 10
        },
        "assumptions_made": [], "confidence": 0.99, "unresolved_flags": [],
    })
    result = _evaluate(gates, raw)
    assert result.contract_passed is False
    assert result.violation_reprompt.rule == "security_checklist_complete"
    # The re-prompt names the exact missing canonical keys (not the empty list the old
    # duplicate-count path produced).
    assert "secrets" in result.violation_reprompt.missing_checklist_categories


def test_security_checklist_duplicate_categories_fail(gates: GateEvaluator) -> None:
    # Ten assessed entries all of the SAME canonical category must NOT satisfy the gate —
    # completeness is by category identity (all ten keys), not row count.
    raw = json.dumps({
        "output": {
            "verdict": "pass", "summary": "s", "findings": [],
            "checklist": [
                {"category": "injection", "assessed": True, "result": "clean", "notes": "n"}
                for _ in range(10)
            ],
        },
        "assumptions_made": [], "confidence": 0.99, "unresolved_flags": [],
    })
    result = _evaluate(gates, raw)
    assert result.contract_passed is False
    assert result.violation_reprompt.rule == "security_checklist_complete"


# ---------------------------------------------------------------------------
# coverage_update_present — pass verdict must record coverage
# ---------------------------------------------------------------------------

def test_coverage_update_empty_on_pass_fails(gates: GateEvaluator) -> None:
    raw = json.dumps({
        "output": {
            "verdict": "pass", "summary": "s",
            "failure_analyses": [], "coverage_update": [],
        },
        "assumptions_made": [], "confidence": 0.99, "unresolved_flags": [],
    })
    result = gates.evaluate(
        raw=raw, expected_model=TestAnalystOutput, agent_id="test_analyst",
        attempt_number=0, assembly_id="a", counters=RetryCounters(), agent_call_count=1,
    )
    assert result.contract_passed is False
    assert result.violation_reprompt.rule == "coverage_update_present"
