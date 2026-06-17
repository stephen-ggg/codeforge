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


def _init_source_repo(source_path: Path, *, initial_branch: str) -> git.Repo:
    source_path.mkdir(parents=True, exist_ok=True)
    repo = git.Repo.init(source_path, initial_branch=initial_branch)
    repo.config_writer().set_value("user", "name", "Test").release()
    repo.config_writer().set_value("user", "email", "test@example.com").release()
    return repo


def _bootstrap_source_repo(source_path: Path) -> git.Repo:
    """A correctly set-up source repo: on `main` with a base commit (as /new-project does)."""
    repo = _init_source_repo(source_path, initial_branch="main")
    repo.git.commit("--allow-empty", "-m", "chore: initialize repository")
    return repo


def _no_worktrees_left(repo: git.Repo) -> bool:
    return "source-worktree" not in repo.git.worktree("list")


def test_commit_source_code_bootstrapped_local_only(
    minimal_config: ConfigSnapshot,
    tmp_path: Path,
) -> None:
    """Healthy local-only project: feature built in a worktree off main, main
    fast-forwarded to include it, repo left resting on main, worktree torn down."""
    source_path = tmp_path / "source"
    repo = _bootstrap_source_repo(source_path)
    base_sha = repo.head.commit.hexsha

    config = _config_with_source_repo(minimal_config, source_path, remote="")
    writer = CommitWriter(config, source_path, run_log_dir=tmp_path / "run-logs")
    result = writer.commit_source_code(_source_commit_input())

    assert result.success is True, result.error_message
    assert result.pr_url is None
    # Canonical checkout rests on main, advanced (fast-forward) to the feature commit.
    assert repo.active_branch.name == "main"
    assert repo.head.commit.hexsha == result.commit_sha
    assert repo.head.commit.hexsha != base_sha
    # Feature branch preserved (for the PR), distinct from main's name.
    assert "codeforge/run-abc123" in [h.name for h in repo.heads]
    # Files present on main, written verbatim — no src/src or tests/tests duplication.
    assert (source_path / "src" / "math.py").exists()
    assert (source_path / "tests" / "test_math.py").exists()
    assert not (source_path / "src" / "src").exists()
    assert not (source_path / "tests" / "tests").exists()
    # No leftover worktree.
    assert _no_worktrees_left(repo)


def test_commit_source_code_not_bootstrapped_errors(
    minimal_config: ConfigSnapshot,
    tmp_path: Path,
) -> None:
    """A repo with no commit at all (unborn HEAD) is a setup error — fail loud."""
    source_path = tmp_path / "source"
    repo = _init_source_repo(source_path, initial_branch="main")  # no commit yet

    config = _config_with_source_repo(minimal_config, source_path, remote="")
    writer = CommitWriter(config, source_path, run_log_dir=tmp_path / "run-logs")
    result = writer.commit_source_code(_source_commit_input())

    assert result.success is False
    assert "main" in (result.error_message or "")
    assert "bootstrap" in (result.error_message or "").lower()
    assert _no_worktrees_left(repo)


def test_commit_source_code_missing_main_errors(
    minimal_config: ConfigSnapshot,
    tmp_path: Path,
) -> None:
    """Reproduces the release-notes corruption: commits exist on a leftover
    codeforge/run-* branch with no `main`. We refuse to mint main from HEAD."""
    source_path = tmp_path / "source"
    repo = _init_source_repo(source_path, initial_branch="codeforge/run-old")
    (source_path / "f.txt").write_text("prior work\n")
    repo.git.add("-A")
    repo.git.commit("-m", "prior work")

    config = _config_with_source_repo(minimal_config, source_path, remote="")
    writer = CommitWriter(config, source_path, run_log_dir=tmp_path / "run-logs")
    result = writer.commit_source_code(_source_commit_input())

    assert result.success is False
    assert "main" in (result.error_message or "")
    # Did not silently create main.
    assert "main" not in [h.name for h in repo.heads]


def test_commit_source_code_failure_leaves_main_clean(
    minimal_config: ConfigSnapshot,
    tmp_path: Path,
) -> None:
    """A push failure (unreachable remote) must escalate with main left pristine and
    no generated files leaked onto the canonical checkout."""
    source_path = tmp_path / "source"
    repo = _bootstrap_source_repo(source_path)
    base_sha = repo.head.commit.hexsha

    config = _config_with_source_repo(
        minimal_config, source_path, remote="file:///nonexistent/repo.git"
    )
    writer = CommitWriter(config, source_path, run_log_dir=tmp_path / "run-logs")
    result = writer.commit_source_code(_source_commit_input())

    assert result.success is False
    # main untouched: still checked out at the original base commit.
    assert repo.active_branch.name == "main"
    assert repo.head.commit.hexsha == base_sha
    assert not (source_path / "src" / "math.py").exists()
    assert _no_worktrees_left(repo)


def test_commit_source_code_resume_after_push_failure(
    minimal_config: ConfigSnapshot,
    tmp_path: Path,
) -> None:
    """Resume: attempt 1 commits the branch then fails at push (main not advanced);
    attempt 2 reuses the existing branch and fast-forwards main — idempotent."""
    source_path = tmp_path / "source"
    repo = _bootstrap_source_repo(source_path)
    base_sha = repo.head.commit.hexsha
    run_logs = tmp_path / "run-logs"

    bad = _config_with_source_repo(
        minimal_config, source_path, remote="file:///nonexistent/repo.git"
    )
    r1 = CommitWriter(bad, source_path, run_log_dir=run_logs).commit_source_code(
        _source_commit_input()
    )
    assert r1.success is False
    # Branch was created + committed before the push failed; main untouched.
    assert "codeforge/run-abc123" in [h.name for h in repo.heads]
    assert repo.head.commit.hexsha == base_sha

    good = _config_with_source_repo(minimal_config, source_path, remote="")
    r2 = CommitWriter(good, source_path, run_log_dir=run_logs).commit_source_code(
        _source_commit_input()
    )
    assert r2.success is True, r2.error_message
    assert repo.active_branch.name == "main"
    assert repo.head.commit.hexsha == r2.commit_sha
    assert (source_path / "src" / "math.py").exists()
    assert _no_worktrees_left(repo)
