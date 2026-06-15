from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from codeforge.orchestrator.event_log import EventLog
from codeforge.orchestrator.gates import GateEvaluator
from codeforge.schemas.contracts import RetryCounters, SecurityReviewerOutput
from codeforge.schemas.validation import OutputValidator

_CONFIG: dict[str, Any] = {
    "confidence_thresholds": {"security_reviewer": 0.90},
    "global_ceiling": {"max_agent_calls_per_run": 40},
}


@pytest.fixture
def gates(tmp_path: Path) -> GateEvaluator:
    validator = OutputValidator(_CONFIG)
    event_log = EventLog(tmp_path / "run", "run-test", "0.0.0")
    return GateEvaluator(validator, event_log, _CONFIG)


def _security_envelope(finding: dict[str, Any], verdict: str = "pass") -> str:
    return json.dumps({
        "output": {
            "verdict": verdict,
            "summary": "test",
            "findings": [finding],
            "checklist": [],
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
