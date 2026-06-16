"""
CommitWriter unit tests (mocked git).

Guards the git operation contract: commit_codeforge_state stages only
project-state/, commits with a message containing run_id, and returns
CommitWriterResult(success=True). No real git repo required.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import git

from codeforge.agents.commit_writer import CommitWriter
from codeforge.config.config_loader import (
    CodeforgeStateRepoConfig,
    ConfigSnapshot,
    ReposConfig,
    SourceCodeRepoConfig,
)
from codeforge.schemas.contracts import (
    CodeArtifact,
    CodeFile,
    CommitWriterInput,
    TestCase,
    TestSuite,
)


@pytest.fixture
def commit_input() -> CommitWriterInput:
    return CommitWriterInput(
        target="codeforge_state",
        run_id="run-abc123",
        codeforge_version="codeforge-v1",
        feature_title="Sum of even numbers",
        ac_ids=["AC-001", "AC-002"],
    )


def test_commit_codeforge_state_success(
    minimal_config: ConfigSnapshot,
    project_dir: Path,
    commit_input: CommitWriterInput,
) -> None:
    mock_repo = MagicMock()
    mock_commit = MagicMock()
    mock_commit.hexsha = "deadbeef"
    mock_repo.index.diff.return_value = [MagicMock()]  # non-empty → changes exist
    mock_repo.index.commit.return_value = mock_commit

    with patch("git.Repo", return_value=mock_repo):
        writer = CommitWriter(minimal_config, project_dir)
        result = writer.commit_codeforge_state(commit_input)

    mock_repo.git.add.assert_called_once_with("project-state/")
    commit_msg = mock_repo.index.commit.call_args[0][0]
    assert "run-abc123" in commit_msg
    assert result.success is True
    assert result.commit_sha == "deadbeef"


def test_commit_codeforge_state_no_changes(
    minimal_config: ConfigSnapshot,
    project_dir: Path,
    commit_input: CommitWriterInput,
) -> None:
    mock_repo = MagicMock()
    mock_repo.head.is_valid.return_value = True
    mock_repo.index.diff.return_value = []
    mock_repo.untracked_files = []
    mock_repo.head.commit.hexsha = "existing-sha"

    with patch("git.Repo", return_value=mock_repo):
        writer = CommitWriter(minimal_config, project_dir)
        result = writer.commit_codeforge_state(commit_input)

    mock_repo.index.commit.assert_not_called()
    assert result.success is True
    assert result.commit_sha == "existing-sha"


def test_commit_codeforge_state_unborn_head(
    minimal_config: ConfigSnapshot,
    project_dir: Path,
    commit_input: CommitWriterInput,
) -> None:
    """First run of a project: state repo has no initial commit (HEAD unborn).

    The diff-against-HEAD short-circuit must be skipped (diffing an unborn HEAD
    raises git.BadName), and the staged files committed as the initial commit.
    """
    mock_repo = MagicMock()
    mock_repo.head.is_valid.return_value = False
    # Diffing an unborn HEAD raises in real GitPython; ensure we never call it.
    mock_repo.index.diff.side_effect = AssertionError("must not diff an unborn HEAD")
    mock_commit = MagicMock()
    mock_commit.hexsha = "root-sha"
    mock_repo.index.commit.return_value = mock_commit

    with patch("git.Repo", return_value=mock_repo):
        writer = CommitWriter(minimal_config, project_dir)
        result = writer.commit_codeforge_state(commit_input)

    mock_repo.git.add.assert_called_once_with("project-state/")
    mock_repo.index.diff.assert_not_called()
    assert result.success is True
    assert result.commit_sha == "root-sha"


def _source_commit_input() -> CommitWriterInput:
    code_artifact = CodeArtifact(
        files=[CodeFile(path="src/math.py", content="def add(a, b):\n    return a + b\n",
                        language="python", change_type="new")],
        change_summary="add",
        criteria_addressed=["AC-001"],
        interface_changes=[],
    )
    test_suite = TestSuite(
        test_cases=[TestCase(
            id="T-001", title="add", criterion_ids=["AC-001"], type="unit",
            description="adds", explicitly_not_testing=[],
            code=[CodeFile(path="tests/test_math.py", content="def test_add():\n    assert True\n",
                           language="python", change_type="new")],
        )],
        test_infrastructure=[],
        coverage_map=[],
    )
    return CommitWriterInput(
        target="source_code",
        run_id="run-abc123",
        codeforge_version="codeforge-v1",
        feature_title="add",
        ac_ids=["AC-001"],
        source_code={
            "code_artifact": code_artifact.model_dump(),
            "test_suite": test_suite.model_dump(),
        },
    )


def _config_with_source_repo(base: ConfigSnapshot, source_path: Path, remote: str) -> ConfigSnapshot:
    return base.model_copy(update={"repos": ReposConfig(
        codeforge_state=CodeforgeStateRepoConfig(remote=""),
        source_code=SourceCodeRepoConfig(path=str(source_path), remote=remote),
    )})


def test_commit_source_code_unborn_head_local_only(
    minimal_config: ConfigSnapshot,
    tmp_path: Path,
) -> None:
    """First run, local-only project: source repo has no commits and no remote.

    The default-branch checkout must be skipped (it errors on an unborn HEAD), the
    feature branch created off the unborn HEAD, files committed, and push/PR skipped
    because the remote is empty.
    """
    source_path = tmp_path / "source"
    source_path.mkdir()
    repo = git.Repo.init(source_path)
    repo.config_writer().set_value("user", "name", "Test").release()
    repo.config_writer().set_value("user", "email", "test@example.com").release()

    config = _config_with_source_repo(minimal_config, source_path, remote="")
    writer = CommitWriter(config, source_path)
    result = writer.commit_source_code(_source_commit_input())

    assert result.success is True, result.error_message
    assert result.commit_sha is not None
    assert result.pr_url is None
    assert repo.active_branch.name == "codeforge/run-abc123"
    # Paths written verbatim — no src/src or tests/tests duplication.
    assert (source_path / "src" / "math.py").exists()
    assert (source_path / "tests" / "test_math.py").exists()
    assert not (source_path / "src" / "src").exists()
    assert not (source_path / "tests" / "tests").exists()
