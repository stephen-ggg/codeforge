"""
Test runner error_phase tests.

Guards the deterministic error classification the runner stamps on an
overall_status="error" result — the signal that drives auto-recovery routing to the
agent that owns the fix. No Docker needed: the pure result-construction helpers are
exercised directly.
"""
from __future__ import annotations

import json

from codeforge.agents.test_runner import _error_result, _parse_pytest_report
from codeforge.schemas.contracts import TestSuite


def _empty_suite() -> TestSuite:
    return TestSuite(test_cases=[], test_infrastructure=[], coverage_map=[])


def test_error_result_stamps_phase() -> None:
    res = _error_result("t0", "img", "runtime_dep_install_failed", stderr="pip boom")
    assert res.overall_status == "error"
    assert res.error_phase == "runtime_dep_install_failed"
    assert res.stderr_tail == "pip boom"


def test_parse_report_non_0_1_exit_is_pytest_exit_error() -> None:
    raw = json.dumps({"exitcode": 2, "tests": []})
    res = _parse_pytest_report(raw, "t0", "img", "collection error", _empty_suite())
    assert res.overall_status == "error"
    assert res.error_phase == "pytest_exit_error"


def test_parse_report_bad_json_is_results_parse_error() -> None:
    res = _parse_pytest_report("not json", "t0", "img", "stdout", _empty_suite())
    assert res.overall_status == "error"
    assert res.error_phase == "results_parse_error"


def test_parse_report_pass_has_no_phase() -> None:
    raw = json.dumps({"exitcode": 0, "tests": []})
    res = _parse_pytest_report(raw, "t0", "img", "", _empty_suite())
    assert res.overall_status == "pass"
    assert res.error_phase is None
