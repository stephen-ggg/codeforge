"""
agents/test_runner.py — Mechanical codeforge test execution agent.

Runs the test suite in a Docker sandbox. No LLM involved.

Container layout (fixed, not configurable per run in MVP):
  /workspace/requirements.txt       — runtime deps (from code_artifact)
  /workspace/requirements-test.txt  — test-only deps (from test_infrastructure, optional)
  /workspace/src/<path>             — source files (from code_artifact, excluding requirements.txt)
  /workspace/tests/<path>           — test files (test_cases[].code + test_infrastructure files)

Exit conventions:
  overall_status: "pass"  — all tests passed (pytest exit 0)
  overall_status: "fail"  — tests ran, some failed (pytest exit 1)
  overall_status: "error" — infrastructure failure: missing requirements.txt, pip install failed,
                            Docker exec failure, missing results.json, or non-0/1 pytest exit code.

InfrastructureError is raised (not returned) when the sandbox image is absent — the
orchestrator routes this to the infrastructure counter rather than the test_loop counter.
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, cast

import docker
import docker.errors

from codeforge.config.config_loader import ConfigSnapshot
from codeforge.schemas.contracts import (
    CodeArtifact,
    TestResult,
    TestRunnerInput,
    TestRunnerResults,
    TestSuite,
)


class InfrastructureError(Exception):
    """Raised when the Docker sandbox cannot be started (image absent, daemon unreachable)."""


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------

class TestRunner:
    def __init__(self, config: ConfigSnapshot) -> None:
        self._config = config

    def run(self, input: TestRunnerInput) -> TestRunnerResults:
        started_at = _now()
        sandbox_image = (
            input.run_config.get("sandbox_image")
            or self._config.test_runner.sandbox_image
        )

        # requirements.txt must be present — fail fast before touching Docker
        has_req = any(f.path == "requirements.txt" for f in input.code_artifact.files)
        if not has_req:
            return _error_result(
                started_at, sandbox_image,
                stderr="Missing requirements.txt in code_artifact",
            )

        try:
            client = docker.DockerClient.from_env()  # type: ignore[attr-defined]
        except Exception as exc:
            raise InfrastructureError(f"Cannot connect to Docker daemon: {exc}") from exc

        try:
            client.images.get(sandbox_image)
        except docker.errors.ImageNotFound:
            raise InfrastructureError(f"Sandbox image not found: {sandbox_image!r}")

        container = None
        try:
            container = client.containers.create(
                image=sandbox_image,
                command="sleep infinity",
                working_dir="/workspace",
            )
            container.start()

            # Ensure workspace directories exist
            container.exec_run("mkdir -p /workspace/src /workspace/tests")

            # Stage files into container
            _copy_code_files(container, input.code_artifact)
            has_req_test = _copy_test_files(container, input.test_suite)

            # Install runtime deps (fail fast — error if this fails)
            exit_code, out = container.exec_run(
                "pip install --quiet -r /workspace/requirements.txt",
                workdir="/workspace",
            )
            if exit_code != 0:
                return _error_result(
                    started_at, sandbox_image,
                    stderr=_decode(out)[-4096:],
                )

            # Install test-only deps after runtime deps
            if has_req_test:
                container.exec_run(
                    "pip install --quiet -r /workspace/requirements-test.txt",
                    workdir="/workspace",
                )

            # Run pytest with JSON report
            _, out = container.exec_run(
                "pytest tests/ --json-report --json-report-file=/workspace/results.json -v",
                workdir="/workspace",
            )
            pytest_stdout = _decode(out)

            # Extract results.json from container
            results_raw = _extract_file(container, "/workspace/results.json")

            if results_raw is None:
                return _error_result(
                    started_at, sandbox_image,
                    stdout=pytest_stdout[-4096:],
                    stderr="pytest did not produce results.json",
                )

            return _parse_pytest_report(
                results_raw,
                started_at,
                sandbox_image,
                pytest_stdout,
                input.test_suite,
            )

        finally:
            if container is not None:
                try:
                    container.stop(timeout=5)
                    container.remove(force=True)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# File staging helpers
# ---------------------------------------------------------------------------

def _copy_code_files(container: Any, code_artifact: CodeArtifact) -> None:
    entries: list[tuple[str, str]] = []
    for f in code_artifact.files:
        if f.change_type == "deleted":
            continue
        if f.path == "requirements.txt":
            entries.append(("workspace/requirements.txt", f.content))
        else:
            entries.append((f"workspace/src/{f.path}", f.content))
    if entries:
        container.put_archive("/", _make_tar(entries))


def _copy_test_files(container: Any, test_suite: TestSuite) -> bool:
    """Stage test files; returns True if requirements-test.txt was found."""
    entries: list[tuple[str, str]] = []
    has_req_test = False

    for test_case in test_suite.test_cases:
        for f in test_case.code:
            entries.append((f"workspace/tests/{f.path}", f.content))

    for f in test_suite.test_infrastructure:
        if f.path == "requirements-test.txt":
            entries.append(("workspace/requirements-test.txt", f.content))
            has_req_test = True
        else:
            entries.append((f"workspace/tests/{f.path}", f.content))

    if entries:
        container.put_archive("/", _make_tar(entries))
    return has_req_test


def _make_tar(entries: list[tuple[str, str]]) -> bytes:
    """Build an in-memory tar archive from (container_path, content) pairs."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for path, content in entries:
            encoded = content.encode("utf-8")
            info = tarfile.TarInfo(name=path)
            info.size = len(encoded)
            tf.addfile(info, io.BytesIO(encoded))
    return buf.getvalue()


def _extract_file(container: Any, container_path: str) -> str | None:
    """Pull a single file's text content out of a running container."""
    try:
        bits, _ = container.get_archive(container_path)
        buf = io.BytesIO()
        for chunk in bits:
            buf.write(chunk)
        buf.seek(0)
        with tarfile.open(fileobj=buf) as tf:
            members = tf.getmembers()
            if not members:
                return None
            member_file = tf.extractfile(members[0])
            if member_file is None:
                return None
            return member_file.read().decode("utf-8", errors="replace")
    except docker.errors.NotFound:
        return None


# ---------------------------------------------------------------------------
# Result construction helpers
# ---------------------------------------------------------------------------

def _parse_pytest_report(
    results_raw: str,
    started_at: str,
    sandbox_image: str,
    pytest_stdout: str,
    test_suite: TestSuite,
) -> TestRunnerResults:
    try:
        data = json.loads(results_raw)
    except json.JSONDecodeError:
        return _error_result(
            started_at, sandbox_image,
            stdout=pytest_stdout[-4096:],
            stderr="Failed to parse results.json as JSON",
        )

    # Build file path → test_case_id lookup so we can map pytest nodeids back to TC ids
    path_to_case_id: dict[str, str] = {}
    for tc in test_suite.test_cases:
        for f in tc.code:
            path_to_case_id[f.path] = tc.id

    _status_map = {
        "passed": "pass",
        "failed": "fail",
        "error": "error",
        "skipped": "skipped",
    }

    test_results: list[TestResult] = []
    for test in data.get("tests", []):
        nodeid: str = test.get("nodeid", "")
        outcome: str = test.get("outcome", "error")
        duration_ms: float = test.get("duration", 0.0) * 1000
        status = cast(
            Literal["pass", "fail", "error", "skipped"],
            _status_map.get(outcome, "error"),
        )

        # Best-effort: extract file part from nodeid to resolve test_case_id
        file_part = nodeid.split("::")[0] if "::" in nodeid else nodeid
        rel_path = file_part.removeprefix("tests/").lstrip("/")
        test_case_id = path_to_case_id.get(rel_path, nodeid)

        # Error detail from pytest's call block
        call = test.get("call", {})
        longrepr = call.get("longrepr")
        error_message: str | None = None
        stack_trace: str | None = None
        if longrepr and status in ("fail", "error"):
            if isinstance(longrepr, str):
                lines = longrepr.splitlines()
                error_message = lines[-1] if lines else longrepr
                stack_trace = longrepr
            elif isinstance(longrepr, dict):
                crash = longrepr.get("reprcrash", {})
                error_message = str(crash.get("message", ""))
                stack_trace = json.dumps(longrepr)

        test_results.append(TestResult(
            test_case_id=test_case_id,
            status=status,
            duration_ms=duration_ms,
            error_message=error_message,
            stack_trace=stack_trace,
            failed_assertions=None,
        ))

    exit_code: int = data.get("exitcode", 1)
    if exit_code == 0:
        overall_status: Literal["pass", "fail", "error"] = "pass"
    elif exit_code == 1:
        overall_status = "fail"
    else:
        overall_status = "error"

    env = data.get("environment", {})
    runtime_version = env.get("Python", env.get("python", ""))

    return TestRunnerResults(
        run_id=str(uuid.uuid4()),
        started_at=started_at,
        completed_at=_now(),
        overall_status=overall_status,
        test_results=test_results,
        environment_info={"sandbox_image": sandbox_image, "runtime_version": runtime_version},
        stdout_tail=pytest_stdout[-4096:],
        stderr_tail="",
    )


def _error_result(
    started_at: str,
    sandbox_image: str,
    stdout: str = "",
    stderr: str = "",
) -> TestRunnerResults:
    return TestRunnerResults(
        run_id=str(uuid.uuid4()),
        started_at=started_at,
        completed_at=_now(),
        overall_status="error",
        test_results=[],
        environment_info={"sandbox_image": sandbox_image, "runtime_version": ""},
        stdout_tail=stdout,
        stderr_tail=stderr,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode(raw: bytes | None) -> str:
    if raw is None:
        return ""
    return raw.decode("utf-8", errors="replace")
