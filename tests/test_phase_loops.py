"""Phase-loop real-path integration tests.

These are the only tests that EXECUTE the StateMachine.run_* phase methods end to
end. Everything else exercises the pieces in isolation (routing functions, gate
evaluation, individual escalation handlers) but never proves they are wired
together correctly inside a phase loop.

Strategy: drive each phase with a REAL gate evaluation but a stubbed model call —
`_invoke_agent` returns a canned JSON envelope, `assemble` returns an empty context
package, and the agent's `build_user_turn` is short-circuited. The full
invoke → gate → route → apply → (re-prompt | advance) loop runs for real.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from codeforge.config.config_loader import ConfigSnapshot
from codeforge.firewall.assembler import ContextPackage
from codeforge.orchestrator.state_machine import StateMachine, _ReviewFailed
from codeforge.schemas.contracts import AcceptanceCriterion, RequirementsDoc


@pytest.fixture
def sm(minimal_config: ConfigSnapshot, project_dir: Path, run_log_dir: Path) -> StateMachine:
    machine = StateMachine(minimal_config, project_dir, run_log_dir)
    machine.start_run("new_project", "a brief")
    return machine


def _req_doc(*must_ids: str) -> RequirementsDoc:
    return RequirementsDoc(
        run_id="run-1", run_mode="new_project", feature_title="t", feature_description="d",
        scope={"in_scope": [], "explicitly_out_of_scope": []},
        acceptance_criteria=[
            AcceptanceCriterion(id=i, description="d", testable=True, priority="must")
            for i in must_ids
        ],
        data_contracts=[], human_confirmed_decisions=[],
    )


def _wire(
    sm: StateMachine,
    monkeypatch: pytest.MonkeyPatch,
    agent_cls: type,
    invoke: Callable[..., str] | str,
) -> None:
    """Stub the model boundary so the real gate/route loop runs against canned output."""
    monkeypatch.setattr(agent_cls, "build_user_turn", lambda self, pkg, reprompt=None: "turn")
    monkeypatch.setattr(
        sm.assembler, "assemble",
        lambda *a, **k: ContextPackage(agent_id=a[0], run_id=a[1], assembly_id="asm-1"),
    )
    if callable(invoke):
        monkeypatch.setattr(sm, "_invoke_agent", lambda *a, **k: invoke())
    else:
        monkeypatch.setattr(sm, "_invoke_agent", lambda *a, **k: invoke)


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------

def _review_env(verdict: str, recorded_acs: list[str], findings: list | None = None) -> str:
    return json.dumps({
        "output": {
            "verdict": verdict, "summary": "s",
            "findings": findings or [],
            "criteria_coverage": [
                {"criterion_id": ac, "addressed": True, "notes": "n"} for ac in recorded_acs
            ],
        },
        "assumptions_made": [], "confidence": 0.99, "unresolved_flags": [],
    })


def _security_env(verdict: str = "pass", findings: list | None = None) -> str:
    checklist = [
        {"category": cat, "assessed": True, "result": "not_applicable", "notes": "n"}
        for cat in (
            "injection", "secrets", "input_validation", "authentication", "authorisation",
            "dependency_vulnerabilities", "sensitive_data_exposure", "xss",
            "insecure_direct_object_references", "error_handling",
        )
    ]
    return json.dumps({
        "output": {
            "verdict": verdict, "summary": "s",
            "findings": findings or [], "checklist": checklist,
        },
        "assumptions_made": [], "confidence": 0.99, "unresolved_flags": [],
    })


def _test_designer_env(ac_ids: list[str]) -> str:
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
        for i, ac in enumerate(ac_ids)
    ]
    return json.dumps({
        "output": {
            "test_cases": cases, "test_infrastructure": [],
            "coverage_map": [
                {"criterion_id": ac, "test_case_ids": [f"TC-{i:03d}"]}
                for i, ac in enumerate(ac_ids)
            ],
        },
        "assumptions_made": [], "confidence": 0.9, "unresolved_flags": [],
    })


def _test_analyst_env(verdict: str = "pass", coverage: bool = True) -> str:
    return json.dumps({
        "output": {
            "verdict": verdict, "summary": "s", "failure_analyses": [],
            "coverage_update": (
                [{"criterion_id": "AC-001", "test_case_ids": ["TC-001"],
                  "status": "covered", "notes": "n"}] if coverage else []
            ),
        },
        "assumptions_made": [], "confidence": 0.99, "unresolved_flags": [],
    })


def _coder_env(ac_ids: list[str]) -> str:
    return json.dumps({
        "output": {
            "files": [
                {"path": "app.py", "content": "x", "language": "python",
                 "change_type": "new", "change_reason": None},
                {"path": "requirements.txt", "content": "", "language": "text",
                 "change_type": "new", "change_reason": None},
            ],
            "module_interfaces": {"files": []},
            "change_summary": "s",
            "criteria_addressed": ac_ids,
            "interface_changes": [],
        },
        "assumptions_made": [], "confidence": 0.9, "unresolved_flags": [],
    })


# ---------------------------------------------------------------------------
# run_code_review
# ---------------------------------------------------------------------------

def test_run_code_review_pass_advances_and_stores(sm: StateMachine, monkeypatch: pytest.MonkeyPatch) -> None:
    from codeforge.agents.code_reviewer import CodeReviewerAgent
    req = _req_doc("AC-001", "AC-002")
    _wire(sm, monkeypatch, CodeReviewerAgent, _review_env("pass", ["AC-001", "AC-002"]))

    report = sm.run_code_review(req, None, None, "sys")  # type: ignore[arg-type]

    assert report.verdict == "pass"
    assert "review_report" in sm.run.artifacts


def test_run_code_review_contract_violation_reprompts_then_succeeds(
    sm: StateMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First response omits a must AC (review_criteria_coverage fails) → one re-prompt on
    the malformed_output budget; the corrected second response advances."""
    from codeforge.agents.code_reviewer import CodeReviewerAgent
    req = _req_doc("AC-001", "AC-002")
    bad = _review_env("pass", ["AC-001"])           # omits AC-002
    good = _review_env("pass", ["AC-001", "AC-002"])
    it = iter([bad, good])
    _wire(sm, monkeypatch, CodeReviewerAgent, lambda: next(it))

    report = sm.run_code_review(req, None, None, "sys")  # type: ignore[arg-type]

    assert report.verdict == "pass"
    assert sm.run.retry_counters.malformed_output == 1  # exactly one re-prompt consumed


def test_run_code_review_fail_verdict_raises_review_failed(
    sm: StateMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codeforge.agents.code_reviewer import CodeReviewerAgent
    req = _req_doc("AC-001")
    finding = {
        "id": "RF1", "file": "app.py", "line_range": [1, 2],
        "category": "correctness", "severity": "warn",
        "description": "x", "suggested_fix": "y",
    }
    _wire(sm, monkeypatch, CodeReviewerAgent, _review_env("fail", ["AC-001"], [finding]))

    with pytest.raises(_ReviewFailed):
        sm.run_code_review(req, None, None, "sys")  # type: ignore[arg-type]


def test_run_code_review_d9_error_severity_forces_fail_through_loop(
    sm: StateMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error-severity finding with verdict 'pass' is forced to 'fail' by D9 inside the
    real phase loop, which then raises _ReviewFailed — proving D9 drives the fail route,
    not just the parsed verdict."""
    from codeforge.agents.code_reviewer import CodeReviewerAgent
    req = _req_doc("AC-001")
    finding = {
        "id": "RF1", "file": "app.py", "line_range": [1, 2],
        "category": "correctness", "severity": "error",
        "description": "x", "suggested_fix": "y",
    }
    _wire(sm, monkeypatch, CodeReviewerAgent, _review_env("pass", ["AC-001"], [finding]))

    with pytest.raises(_ReviewFailed):
        sm.run_code_review(req, None, None, "sys")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# run_security_review
# ---------------------------------------------------------------------------

def test_run_security_review_pass_advances_and_stores(
    sm: StateMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codeforge.agents.security_reviewer import SecurityReviewerAgent
    _wire(sm, monkeypatch, SecurityReviewerAgent, _security_env("pass"))

    report = sm.run_security_review(_req_doc("AC-001"), None, "sys")  # type: ignore[arg-type]

    assert report.verdict == "pass"
    assert "security_report" in sm.run.artifacts


def test_run_security_review_fail_raises_review_failed(
    sm: StateMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codeforge.agents.security_reviewer import SecurityReviewerAgent
    finding = {
        "id": "SF1", "file": "app.py", "line_range": [1, 2],
        "category": "input_validation", "severity": "warn",
        "description": "x", "recommended_fix": "y",
    }
    _wire(sm, monkeypatch, SecurityReviewerAgent, _security_env("fail", [finding]))

    with pytest.raises(_ReviewFailed):
        sm.run_security_review(_req_doc("AC-001"), None, "sys")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# run_test_design
# ---------------------------------------------------------------------------

def test_run_test_design_pass_advances_and_stores(
    sm: StateMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codeforge.agents.test_designer import TestDesignerAgent
    req = _req_doc("AC-001")
    _wire(sm, monkeypatch, TestDesignerAgent, _test_designer_env(["AC-001"]))

    suite = sm.run_test_design(req, None, "sys")  # type: ignore[arg-type]

    assert suite is not None
    assert "test_suite" in sm.run.artifacts


# ---------------------------------------------------------------------------
# run_test_analysis
# ---------------------------------------------------------------------------

def test_run_test_analysis_pass_advances_and_stores(
    sm: StateMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codeforge.agents.test_analyst import TestAnalystAgent
    _wire(sm, monkeypatch, TestAnalystAgent, _test_analyst_env("pass", coverage=True))

    analysis = sm.run_test_analysis(_req_doc("AC-001"), None, {}, "sys")  # type: ignore[arg-type]

    assert analysis.verdict == "pass"
    assert "test_analysis" in sm.run.artifacts


def test_run_test_analysis_pass_without_coverage_reprompts_then_succeeds(
    sm: StateMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 'pass' verdict with empty coverage_update fails the coverage_update_present gate →
    re-prompt; the corrected response (with coverage) advances."""
    from codeforge.agents.test_analyst import TestAnalystAgent
    bad = _test_analyst_env("pass", coverage=False)
    good = _test_analyst_env("pass", coverage=True)
    it = iter([bad, good])
    _wire(sm, monkeypatch, TestAnalystAgent, lambda: next(it))

    analysis = sm.run_test_analysis(_req_doc("AC-001"), None, {}, "sys")  # type: ignore[arg-type]

    assert analysis.verdict == "pass"
    assert sm.run.retry_counters.malformed_output == 1


# ---------------------------------------------------------------------------
# run_coding
# ---------------------------------------------------------------------------

def test_run_coding_valid_advances_and_stores(
    sm: StateMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codeforge.agents.coder import CoderAgent
    req = _req_doc("AC-001")
    _wire(sm, monkeypatch, CoderAgent, _coder_env(["AC-001"]))

    artifact = sm.run_coding(req, None, "sys")  # type: ignore[arg-type]

    assert artifact is not None
    assert "code_artifact" in sm.run.artifacts
    assert "module_interfaces" in sm.run.artifacts
