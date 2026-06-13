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

from codeforge.agents.commit_writer import CommitWriter
from codeforge.config.config_loader import ConfigSnapshot
from codeforge.schemas.contracts import CommitWriterInput


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
    mock_repo.index.diff.return_value = []
    mock_repo.untracked_files = []
    mock_repo.head.commit.hexsha = "existing-sha"

    with patch("git.Repo", return_value=mock_repo):
        writer = CommitWriter(minimal_config, project_dir)
        result = writer.commit_codeforge_state(commit_input)

    mock_repo.index.commit.assert_not_called()
    assert result.success is True
    assert result.commit_sha == "existing-sha"
