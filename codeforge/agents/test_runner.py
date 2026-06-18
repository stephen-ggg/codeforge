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
from codeforge.stacks.profile import StackProfile
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
        profile = self._config.stack_profile
        sandbox_image = (
            input.run_config.get("sandbox_image")
            or self._config.test_runner.sandbox_image
            or profile.default_sandbox_image
        )
        workdir = profile.workdir

        # Resolve the full set of source files to stage. For continuation we start
        # from the existing repo and apply the coder's deltas (new / edits / delete)
        # so tests run against the WHOLE tree, not just the changed files.
        try:
            code_entries = _resolve_code_entries(input, profile)
        except Exception as exc:  # EditError or filesystem error
            return _error_result(
                started_at, sandbox_image,
                stderr=f"Failed to assemble source tree: {exc}",
            )

        # The dependency manifest must be present — fail fast before touching Docker.
        if profile.manifest_required:
            manifest_key = f"workspace/{profile.manifest_filename}"
            has_manifest = any(path == manifest_key for path, _ in code_entries)
            if not has_manifest:
                return _error_result(
                    started_at, sandbox_image, "missing_requirements_txt",
                    stderr=f"Missing {profile.manifest_filename} in staged source tree",
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
                working_dir=workdir,
            )
            container.start()

            # Ensure the workspace root exists. Subdirectories are created automatically by
            # put_archive when staging files, so we don't assume any stack-specific layout.
            container.exec_run(f"mkdir -p {workdir}")

            # Stage files into container
            if code_entries:
                container.put_archive("/", _make_tar(code_entries))
            has_test_manifest = _copy_test_files(container, input.test_suite, profile)

            # Install runtime deps (fail fast — error if this fails).
            err = _run_steps(
                container, profile.install_commands, workdir,
                started_at, sandbox_image, "runtime_dep_install_failed",
            )
            if err is not None:
                return err

            # Install test-only deps after runtime deps, only when the profile uses a
            # separate test manifest and the test_designer staged it. A failed test-dep
            # install otherwise surfaces later as an opaque "no report" error.
            if has_test_manifest and profile.test_install_commands:
                err = _run_steps(
                    container, profile.test_install_commands, workdir,
                    started_at, sandbox_image, "test_dep_install_failed",
                )
                if err is not None:
                    return err

            # Compile/type-check gate (e.g. `tsc --noEmit`). Empty for interpreted stacks.
            # A failure here is a code defect the coder owns.
            err = _run_steps(
                container, profile.build_commands, workdir,
                started_at, sandbox_image, "build_failed",
            )
            if err is not None:
                return err

            # Run the suite, emitting a JUnit XML report (pytest core / vitest --reporter=junit).
            exit_code, out = container.exec_run(profile.test_command, workdir=workdir)
            test_stdout = _decode(out)

            # Extract the JUnit report from the container
            results_raw = _extract_file(container, profile.results_path)

            if results_raw is None:
                return _error_result(
                    started_at, sandbox_image, "no_results_report",
                    stdout=test_stdout[-4096:],
                    stderr=f"test command produced no JUnit XML report (exit={exit_code})",
                )

            return _parse_junit_report(
                results_raw,
                exit_code,
                started_at,
                sandbox_image,
                test_stdout,
                input.test_suite,
                profile.runtime_version_regex,
            )

        finally:
            if container is not None:
                try:
                    container.stop(timeout=5)
                    container.remove(force=True)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Command execution helper
# ---------------------------------------------------------------------------

def _run_steps(
    container: Any,
    commands: list[str],
    workdir: str,
    started_at: str,
    sandbox_image: str,
    error_phase: "TestRunnerErrorPhase",
) -> TestRunnerResults | None:
    """Run a sequence of shell commands in the container, fail-fast.

    Returns an error TestRunnerResults tagged with error_phase on the first non-zero
    exit, or None if every command succeeded. Commands are run via `sh -c` so shell
    operators (e.g. `npm ci || npm install`) work.
    """
    for cmd in commands:
        exit_code, out = container.exec_run(["sh", "-c", cmd], workdir=workdir)
        if exit_code != 0:
            return _error_result(
                started_at, sandbox_image, error_phase,
                stderr=_decode(out)[-4096:],
            )
    return None


# ---------------------------------------------------------------------------
# File staging helpers
# ---------------------------------------------------------------------------

def _stageable(path: Path) -> bool:
    try:
        return path.is_file() and b"\x00" not in path.read_bytes()[:4096]
    except OSError:
        return False


def _resolve_code_entries(input: TestRunnerInput, profile: StackProfile) -> list[tuple[str, str]]:
    """Return (container_path, content) pairs for the source tree to stage.

    new_project: just the code_artifact files (current behaviour).
    continuation: the existing repo (the profile's source_globs) with the coder's
    deltas applied — new files written, modified files patched via edits, deleted
    files removed — so the sandbox holds the complete post-change tree.
    """
    files: dict[str, str] = {}  # workspace-relative key, e.g. "src/foo.py" / "package.json"

    if input.run_mode == "continuation" and input.source_root:
        root = Path(input.source_root)
        for rel in _expand_globs(root, profile.source_globs):
            p = root / rel
            if _stageable(p):
                files[rel] = p.read_text(errors="replace")

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


def _expand_globs(root: Path, patterns: list[str]) -> list[str]:
    """Expand the profile's source_globs under root into root-relative posix file paths.

    Patterns ending in `/**` are treated as recursive directory matches (their whole
    subtree of files is staged); other patterns are passed to Path.glob directly. Only
    regular files are returned — directory entries are filtered out by the caller's
    _stageable check, but we also skip them here to keep the list clean.
    """
    found: dict[str, None] = {}  # ordered set of relative posix paths
    for pattern in patterns:
        glob_pattern = f"{pattern}/*" if pattern.endswith("/**") else pattern
        for p in root.glob(glob_pattern):
            if p.is_file():
                found[p.relative_to(root).as_posix()] = None
    return list(found)


def _copy_test_files(container: Any, test_suite: TestSuite, profile: StackProfile) -> bool:
    """Stage test files verbatim; returns True if the profile's test manifest was staged."""
    entries: list[tuple[str, str]] = []
    has_test_manifest = False
    test_manifest = profile.test_manifest_filename

    for test_case in test_suite.test_cases:
        for f in test_case.code:
            entries.append((_workspace_path(f.path), f.content))

    for f in test_suite.test_infrastructure:
        if test_manifest is not None and f.path == test_manifest:
            has_test_manifest = True
        entries.append((_workspace_path(f.path), f.content))

    if entries:
        container.put_archive("/", _make_tar(entries))
    return has_test_manifest


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
    runtime_version_regex: str | None = None,
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

        test_case_id = _match_case_id(classname, name, path_to_case_id)

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
        environment_info={
            "sandbox_image": sandbox_image,
            "runtime_version": _runtime_version(pytest_stdout, runtime_version_regex),
        },
        stdout_tail=pytest_stdout[-4096:],
        stderr_tail="",
        error_phase=error_phase,
    )


def _match_case_id(classname: str, name: str, path_to_case_id: dict[str, str]) -> str:
    """Map a JUnit <testcase> back to a test_case_id, framework-agnostically.

    Different runners populate `classname` differently: pytest uses a dotted module path
    (`tests.test_add`), vitest uses the test file path (`lib/cards.test.ts`). We try the raw
    classname (vitest), then the pytest dotted→path transform, against the test files' paths.
    Falls back to `classname::name` when nothing matches.
    """
    for candidate in (classname, _classname_to_path(classname or name)):
        if candidate in path_to_case_id:
            return path_to_case_id[candidate]
    return f"{classname}::{name}" if classname else name


def _classname_to_path(classname: str) -> str:
    """JUnit dotted module ('tests.sub.test_x') → root-relative path ('tests/sub/test_x.py').

    pytest's rootdir is /workspace, so the classname is the module path relative to it —
    which matches the project-root-relative paths the test files are staged and keyed under.
    """
    module_path = classname.replace(".", "/")
    if not module_path.endswith(".py"):
        module_path += ".py"
    return module_path


def _runtime_version(test_stdout: str, regex: str | None) -> str:
    """Best-effort runtime version from the test command's stdout, per the profile's regex.

    For Python this matches pytest's session header ('-- Python 3.12.13'). Returns "" when
    the profile defines no regex or the pattern does not match.
    """
    if not regex:
        return ""
    match = re.search(regex, test_stdout)
    return match.group(1) if match else ""


def _error_result(
    started_at: str,
    sandbox_image: str,
    error_phase: "TestRunnerErrorPhase | None" = None,
    stdout: str = "",
    stderr: str = "",
) -> TestRunnerResults:
    """Build an error result. error_phase is optional: an unclassified failure (e.g. the
    source tree could not be assembled) leaves it None, which routing handles by falling back
    to route_test_analysis_error rather than an agent-specific recovery."""
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
