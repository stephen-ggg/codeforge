"""
Test runner error_phase tests.

Guards the deterministic error classification the runner stamps on an
overall_status="error" result — the signal that drives auto-recovery routing to the
agent that owns the fix. No Docker needed: the pure result-construction helpers are
exercised directly.
"""
from __future__ import annotations

from codeforge.agents.test_runner import _classname_to_path, _error_result, _parse_junit_report
from codeforge.schemas.contracts import CodeFile, TestCase, TestSuite

_JUNIT_TWO_TESTS = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="1" skipped="0" tests="2" time="0.03">
  <testcase classname="tests.test_sample" name="test_pass" time="0.001"/>
  <testcase classname="tests.test_sample" name="test_fail" time="0.002">
    <failure message="assert 1 == 2">tests/test_sample.py:7: AssertionError</failure>
  </testcase>
</testsuite></testsuites>"""


def _empty_suite() -> TestSuite:
    return TestSuite(test_cases=[], test_infrastructure=[], coverage_map=[])


def test_error_result_stamps_phase() -> None:
    res = _error_result("t0", "img", "runtime_dep_install_failed", stderr="pip boom")
    assert res.overall_status == "error"
    assert res.error_phase == "runtime_dep_install_failed"
    assert res.stderr_tail == "pip boom"


def test_parse_report_non_0_1_exit_is_pytest_exit_error() -> None:
    # exit 2 = collection interrupted; the JUnit report still exists.
    res = _parse_junit_report(_JUNIT_TWO_TESTS, 2, "t0", "img", "collection error", _empty_suite())
    assert res.overall_status == "error"
    assert res.error_phase == "pytest_exit_error"


def test_parse_report_bad_xml_is_results_parse_error() -> None:
    res = _parse_junit_report("<not valid", 1, "t0", "img", "stdout", _empty_suite())
    assert res.overall_status == "error"
    assert res.error_phase == "results_parse_error"


def test_parse_report_pass_has_no_phase() -> None:
    res = _parse_junit_report(_JUNIT_TWO_TESTS, 0, "t0", "img", "", _empty_suite())
    assert res.overall_status == "pass"
    assert res.error_phase is None


def test_classname_maps_to_root_relative_path() -> None:
    # JUnit classname is rooted at /workspace, matching the project-root-relative paths
    # files are staged under — so the tests/ prefix is preserved, not stripped.
    assert _classname_to_path("tests.test_math_operations") == "tests/test_math_operations.py"
    assert _classname_to_path("tests.sub.test_x") == "tests/sub/test_x.py"


def test_parse_report_resolves_test_case_id_from_classname() -> None:
    suite = TestSuite(
        test_cases=[TestCase(
            id="TC-001", title="t", criterion_ids=["AC-1"], type="unit", description="",
            explicitly_not_testing=[],
            code=[CodeFile(path="tests/test_sample.py", content="", language="python", change_type="new")],
        )],
        test_infrastructure=[], coverage_map=[],
    )
    res = _parse_junit_report(_JUNIT_TWO_TESTS, 0, "t0", "img", "", suite)
    assert {r.test_case_id for r in res.test_results} == {"TC-001"}


def test_parse_report_maps_testcase_outcomes() -> None:
    res = _parse_junit_report(_JUNIT_TWO_TESTS, 1, "t0", "img", "", _empty_suite())
    assert res.overall_status == "fail"
    statuses = {r.status for r in res.test_results}
    assert statuses == {"pass", "fail"}
    failed = next(r for r in res.test_results if r.status == "fail")
    assert failed.error_message == "assert 1 == 2"
    assert "AssertionError" in (failed.stack_trace or "")
