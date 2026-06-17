"""
agents/test_runner.py — Mechanical codeforge test execution agent.

Runs the test suite in a Docker sandbox. No LLM involved.

Container layout (fixed, not configurable per run in MVP):
  /workspace/<path>                 — every file staged verbatim at its project-root-relative
                                      path. The agents already emit root-relative paths
                                      (src/foo.py, tests/test_foo.py, requirements.txt,
                                      conftest.py), so /workspace is a faithful project tree.
                                      pytest is invoked from /workspace; src/ is therefore
                                      importable and tests/ is discoverable.

Results are emitted via pytest core's built-in --junit-xml reporter — no third-party
plugin — so a generated requirements file pinning a different pytest cannot break the
results channel. overall_status is derived from pytest's real process exit code, not from
anything recorded inside the report, so a harness that never starts is distinguishable
from tests that ran and failed.

Exit conventions:
  overall_status: "pass"  — all tests passed (pytest exit 0)
  overall_status: "fail"  — tests ran, some failed (pytest exit 1)
  overall_status: "error" — infrastructure failure: missing requirements.txt, pip install failed,
                            Docker exec failure, missing results.xml, or non-0/1 pytest exit code.

InfrastructureError is raised (not returned) when the sandbox image is absent — the
orchestrator routes this to the infrastructure counter rather than the test_loop counter.
"""

from __future__ import annotations

import io
import re
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from xml.etree import ElementTree

import docker
import docker.errors

from codeforge.config.config_loader import ConfigSnapshot
from codeforge.schemas.contracts import (
    TestResult,
    TestRunnerErrorPhase,
    TestRunnerInput,
    TestRunnerResults,
    TestSuite,
)
from codeforge.store.edits import apply_edits


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

        # Resolve the full set of source files to stage. For continuation we start
        # from the existing repo and apply the coder's deltas (new / edits / delete)
        # so tests run against the WHOLE tree, not just the changed files.
        try:
            code_entries = _resolve_code_entries(input)
        except Exception as exc:  # EditError or filesystem error
            return _error_result(
                started_at, sandbox_image,
                stderr=f"Failed to assemble source tree: {exc}",
            )

        # requirements.txt must be present — fail fast before touching Docker
        has_req = any(path == "workspace/requirements.txt" for path, _ in code_entries)
        if not has_req:
            return _error_result(
                started_at, sandbox_image, "missing_requirements_txt",
                stderr="Missing requirements.txt in staged source tree",
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
            if code_entries:
                container.put_archive("/", _make_tar(code_entries))
            has_req_test = _copy_test_files(container, input.test_suite)

            # Install runtime deps (fail fast — error if this fails)
            exit_code, out = container.exec_run(
                "pip install --quiet -r /workspace/requirements.txt",
                workdir="/workspace",
            )
            if exit_code != 0:
                return _error_result(
                    started_at, sandbox_image, "runtime_dep_install_failed",
                    stderr=_decode(out)[-4096:],
                )

            # Install test-only deps after runtime deps. Check the exit code: a failed
            # test-dep install otherwise surfaces later as an opaque "no report" error.
            if has_req_test:
                exit_code, out = container.exec_run(
                    "pip install --quiet -r /workspace/requirements-test.txt",
                    workdir="/workspace",
                )
                if exit_code != 0:
                    return _error_result(
                        started_at, sandbox_image, "test_dep_install_failed",
                        stderr=_decode(out)[-4096:],
                    )

            # Run pytest, emitting a JUnit XML report via pytest core (no plugin).
            # Invoked as `python -m pytest` (not the console script) so the working
            # directory /workspace lands on sys.path — tests import the app via the
            # `src.` package the manifest/test_designer prescribes, which only resolves
            # when /workspace is importable.
            exit_code, out = container.exec_run(
                "python -m pytest tests/ --junit-xml=/workspace/results.xml -o junit_family=xunit2 -v",
                workdir="/workspace",
            )
            pytest_stdout = _decode(out)

            # Extract results.xml from container
            results_raw = _extract_file(container, "/workspace/results.xml")

            if results_raw is None:
                return _error_result(
                    started_at, sandbox_image, "no_results_report",
                    stdout=pytest_stdout[-4096:],
                    stderr=f"pytest produced no JUnit XML report (exit={exit_code})",
                )

            return _parse_junit_report(
                results_raw,
                exit_code,
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

def _stageable(path: Path) -> bool:
    try:
        return path.is_file() and b"\x00" not in path.read_bytes()[:4096]
    except OSError:
        return False


def _resolve_code_entries(input: TestRunnerInput) -> list[tuple[str, str]]:
    """Return (container_path, content) pairs for the source tree to stage.

    new_project: just the code_artifact files (current behaviour).
    continuation: the existing repo (src/** + requirements.txt) with the coder's
    deltas applied — new files written, modified files patched via edits, deleted
    files removed — so the sandbox holds the complete post-change tree.
    """
    files: dict[str, str] = {}  # workspace-relative key, e.g. "src/foo.py" / "requirements.txt"

    if input.run_mode == "continuation" and input.source_root:
        root = Path(input.source_root)
        src_root = root / "src"
        if src_root.is_dir():
            for p in src_root.rglob("*"):
                if _stageable(p):
                    files[f"src/{p.relative_to(src_root).as_posix()}"] = p.read_text(errors="replace")
        req = root / "requirements.txt"
        if req.is_file():
            files["requirements.txt"] = req.read_text(errors="replace")

    for f in input.code_artifact.files:
        # Coder paths are project-root-relative and verbatim (src/foo.py,
        # requirements.txt) — matching the repo keys above. Do NOT re-prefix.
        key = f.path
        if f.change_type == "deleted":
            files.pop(key, None)
        elif f.change_type == "modified" and f.edits:
            files[key] = apply_edits(files.get(key, ""), f.edits)
        else:
            files[key] = f.content

    return [(f"workspace/{key}", content) for key, content in files.items()]


def _copy_test_files(container: Any, test_suite: TestSuite) -> bool:
    """Stage test files verbatim; returns True if requirements-test.txt was found."""
    entries: list[tuple[str, str]] = []
    has_req_test = False

    for test_case in test_suite.test_cases:
        for f in test_case.code:
            entries.append((_workspace_path(f.path), f.content))

    for f in test_suite.test_infrastructure:
        if f.path == "requirements-test.txt":
            has_req_test = True
        entries.append((_workspace_path(f.path), f.content))

    if entries:
        container.put_archive("/", _make_tar(entries))
    return has_req_test


def _workspace_path(path: str) -> str:
    """Project-root-relative path → tar member path under /workspace (no leading slash)."""
    return f"workspace/{path.lstrip('/')}"


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

def _parse_junit_report(
    results_raw: str,
    exit_code: int,
    started_at: str,
    sandbox_image: str,
    pytest_stdout: str,
    test_suite: TestSuite,
) -> TestRunnerResults:
    try:
        root = ElementTree.fromstring(results_raw)
    except ElementTree.ParseError:
        return _error_result(
            started_at, sandbox_image, "results_parse_error",
            stdout=pytest_stdout[-4096:],
            stderr="Failed to parse results.xml as JUnit XML",
        )

    # Build file path → test_case_id lookup so we can map JUnit classnames back to TC ids
    path_to_case_id: dict[str, str] = {}
    for tc in test_suite.test_cases:
        for f in tc.code:
            path_to_case_id[f.path] = tc.id

    test_results: list[TestResult] = []
    for case in root.iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        duration_ms = float(case.get("time", "0") or 0) * 1000

        rel_path = _classname_to_path(classname or name)
        test_case_id = path_to_case_id.get(rel_path, f"{classname}::{name}" if classname else name)

        failure = case.find("failure")
        error = case.find("error")
        skipped = case.find("skipped")

        status: Literal["pass", "fail", "error", "skipped"]
        error_message: str | None = None
        stack_trace: str | None = None
        if failure is not None:
            status = "fail"
            error_message = failure.get("message")
            stack_trace = failure.text
        elif error is not None:
            status = "error"
            error_message = error.get("message")
            stack_trace = error.text
        elif skipped is not None:
            status = "skipped"
        else:
            status = "pass"

        test_results.append(TestResult(
            test_case_id=test_case_id,
            status=status,
            duration_ms=duration_ms,
            error_message=error_message,
            stack_trace=stack_trace,
            failed_assertions=None,
        ))

    # overall_status is driven by pytest's real process exit code (0 pass, 1 tests failed,
    # any other value = the harness itself did not complete a normal run).
    error_phase: TestRunnerErrorPhase | None = None
    if exit_code == 0:
        overall_status: Literal["pass", "fail", "error"] = "pass"
    elif exit_code == 1:
        overall_status = "fail"
    else:
        overall_status = "error"
        error_phase = "pytest_exit_error"

    return TestRunnerResults(
        run_id=str(uuid.uuid4()),
        started_at=started_at,
        completed_at=_now(),
        overall_status=overall_status,
        test_results=test_results,
        environment_info={"sandbox_image": sandbox_image, "runtime_version": _python_version(pytest_stdout)},
        stdout_tail=pytest_stdout[-4096:],
        stderr_tail="",
        error_phase=error_phase,
    )


def _classname_to_path(classname: str) -> str:
    """JUnit dotted module ('tests.sub.test_x') → root-relative path ('tests/sub/test_x.py').

    pytest's rootdir is /workspace, so the classname is the module path relative to it —
    which matches the project-root-relative paths the test files are staged and keyed under.
    """
    module_path = classname.replace(".", "/")
    if not module_path.endswith(".py"):
        module_path += ".py"
    return module_path


def _python_version(pytest_stdout: str) -> str:
    """Best-effort Python version from pytest's session header (e.g. '-- Python 3.12.13')."""
    match = re.search(r"Python (\d+\.\d+\.\d+)", pytest_stdout)
    return match.group(1) if match else ""


def _error_result(
    started_at: str,
    sandbox_image: str,
    error_phase: "TestRunnerErrorPhase",
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
        error_phase=error_phase,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode(raw: bytes | None) -> str:
    if raw is None:
        return ""
    return raw.decode("utf-8", errors="replace")
