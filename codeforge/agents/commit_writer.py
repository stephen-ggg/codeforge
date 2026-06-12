"""
agents/commit_writer.py — Mechanical pipeline commit agent.

Two operations, no LLM:
  commit_pipeline_state — git add + commit + push the project-state/ directory after the
                          state_writer has already flushed pending_writes to disk.
  commit_source_code    — write src/, tests/, requirements*.txt to the source code repo,
                          create a branch, commit, push, open a GitHub PR.

CommitWriter does NOT increment agent_call_count — the orchestrator skips the ceiling
check for CommitWriter invocations.

Constructor:
    config      — ConfigSnapshot (provides repos config and github_token)
    project_dir — Path to the managed project directory (the pipeline state git repo root)

CommitWriterInput.pipeline_state and .source_code are untyped dicts to avoid a circular
dependency between contracts and the agent layer. Expected shapes:

  pipeline_state: (ignored — files are already on disk when commit_pipeline_state is called)

  source_code: {
      "code_artifact": { ...CodeArtifact fields... },
      "test_suite":    { ...TestSuite fields...    },
  }
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import git
import git.exc

from codeforge.config.config_loader import ConfigSnapshot
from codeforge.schemas.contracts import (
    CodeArtifact,
    CommitWriterInput,
    CommitWriterResult,
    TestSuite,
)


class CommitWriter:
    def __init__(self, config: ConfigSnapshot, project_dir: Path) -> None:
        self._config = config
        self._project_dir = project_dir

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def commit_pipeline_state(self, input: CommitWriterInput) -> CommitWriterResult:
        """
        Commit the project-state/ directory to the pipeline state repo and push.

        Precondition: state_writer.flush_pending_writes() has already written all
        changed JSON + markdown files to disk. This method only does the git work.
        """
        try:
            repo = git.Repo(self._project_dir)

            # Stage only the project-state/ subtree
            repo.git.add("project-state/")

            # Nothing to commit is not an error — maybe this run produced no state changes
            if not repo.index.diff("HEAD") and not repo.untracked_files:
                commit_sha = repo.head.commit.hexsha
                return CommitWriterResult(
                    target="pipeline_state",
                    success=True,
                    commit_sha=commit_sha,
                )

            message = (
                f"chore(pipeline): run {input.run_id} — {input.feature_title}"
            )
            commit = repo.index.commit(message)

            repos_cfg = self._config.repos
            if repos_cfg and repos_cfg.pipeline_state.remote:
                remote = _get_or_add_remote(
                    repo,
                    repos_cfg.pipeline_state.remote,
                    "pipeline_state",
                )
                branch = repos_cfg.pipeline_state.branch
                repo.git.push(remote, f"HEAD:{branch}")

            return CommitWriterResult(
                target="pipeline_state",
                success=True,
                commit_sha=commit.hexsha,
            )

        except Exception as exc:
            return CommitWriterResult(
                target="pipeline_state",
                success=False,
                error_message=str(exc),
            )

    def commit_source_code(self, input: CommitWriterInput) -> CommitWriterResult:
        """
        Write source files to the source repo, create a branch, commit, push, open PR.
        """
        try:
            source_data: dict[str, Any] = input.source_code or {}
            code_artifact = CodeArtifact(**source_data["code_artifact"])
            test_suite = TestSuite(**source_data["test_suite"])

            repos_cfg = self._config.repos
            if repos_cfg is None:
                raise ValueError("repos config block is required for commit_source_code")

            src_cfg = repos_cfg.source_code
            source_repo_path = Path(src_cfg.path)
            repo = git.Repo(source_repo_path)

            # Start from a clean default branch
            repo.git.checkout(src_cfg.default_branch)

            branch_name = f"{src_cfg.branch_prefix}{input.run_id}"
            repo.git.checkout("-b", branch_name)

            # Write source files
            output_dir = src_cfg.output_dir or "src"
            _write_code_artifact(source_repo_path, code_artifact, output_dir)
            _write_test_suite(source_repo_path, test_suite)

            # Commit
            repo.git.add("-A")
            message = f"feat({input.feature_title}): implement {input.feature_title}"
            commit = repo.index.commit(message)
            commit_sha = commit.hexsha

            # Push
            remote = _get_or_add_remote(repo, src_cfg.remote, "origin")
            repo.git.push(remote, f"{branch_name}:{branch_name}", "--set-upstream")

            # Open PR
            pr_url = _open_pull_request(
                github_token=self._config.github_token,
                remote_url=src_cfg.remote,
                branch_name=branch_name,
                pr_target=src_cfg.pr_target,
                auto_merge=src_cfg.auto_merge,
                input=input,
                commit_sha=commit_sha,
            )

            return CommitWriterResult(
                target="source_code",
                success=True,
                commit_sha=commit_sha,
                pr_url=pr_url,
            )

        except Exception as exc:
            return CommitWriterResult(
                target="source_code",
                success=False,
                error_message=str(exc),
            )


# ---------------------------------------------------------------------------
# File writing helpers
# ---------------------------------------------------------------------------

def _write_code_artifact(
    repo_root: Path, code_artifact: CodeArtifact, output_dir: str
) -> None:
    for f in code_artifact.files:
        if f.path == "requirements.txt":
            target = repo_root / "requirements.txt"
        else:
            target = repo_root / output_dir / f.path

        if f.change_type == "deleted":
            if target.exists():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.content, encoding="utf-8")


def _write_test_suite(repo_root: Path, test_suite: TestSuite) -> None:
    for test_case in test_suite.test_cases:
        for f in test_case.code:
            target = repo_root / "tests" / f.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.content, encoding="utf-8")

    for f in test_suite.test_infrastructure:
        if f.path == "requirements-test.txt":
            target = repo_root / "requirements-test.txt"
        else:
            target = repo_root / "tests" / f.path
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f.content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _get_or_add_remote(repo: git.Repo, remote_url: str, preferred_name: str) -> str:
    """Return the name of an existing remote with matching URL, or add one."""
    for remote in repo.remotes:
        if remote.url == remote_url:
            return remote.name
    name = preferred_name
    try:
        repo.create_remote(name, remote_url)
    except git.exc.GitCommandError:
        # Name taken with a different URL — use a unique suffix
        name = f"{preferred_name}_pipeline"
        repo.create_remote(name, remote_url)
    return name


# ---------------------------------------------------------------------------
# GitHub PR helpers
# ---------------------------------------------------------------------------

def _open_pull_request(
    *,
    github_token: str,
    remote_url: str,
    branch_name: str,
    pr_target: str,
    auto_merge: bool,
    input: CommitWriterInput,
    commit_sha: str,
) -> str | None:
    if not github_token:
        return None
    try:
        from github import Github

        repo_name = _extract_github_repo(remote_url)
        g = Github(github_token)
        gh_repo = g.get_repo(repo_name)

        body = _build_pr_description(input, commit_sha)
        pr = gh_repo.create_pull(
            title=f"feat: implement {input.feature_title}",
            body=body,
            head=branch_name,
            base=pr_target,
        )

        if auto_merge:
            try:
                pr.enable_automerge(merge_method="squash")
            except Exception:
                pass  # automerge requires repo setting; ignore if unsupported

        return pr.html_url

    except Exception:
        return None


def _extract_github_repo(remote_url: str) -> str:
    """Extract 'owner/repo' from an https or ssh GitHub remote URL."""
    url = remote_url.strip().removesuffix(".git")
    # ssh: git@github.com:owner/repo
    ssh_match = re.search(r"github\.com[:/](.+/.+)$", url)
    if ssh_match:
        return ssh_match.group(1)
    # https: https://github.com/owner/repo
    https_match = re.search(r"github\.com/(.+/.+)$", url)
    if https_match:
        return https_match.group(1)
    raise ValueError(f"Cannot extract GitHub repo name from remote URL: {remote_url!r}")


def _build_pr_description(input: CommitWriterInput, commit_sha: str) -> str:
    ac_list = "\n".join(f"- `{ac}`" for ac in input.ac_ids) or "_(none specified)_"
    return (
        f"## Pipeline-generated feature\n\n"
        f"**Feature:** {input.feature_title}\n"
        f"**Pipeline run:** `{input.run_id}`\n"
        f"**Pipeline version:** `{input.pipeline_version}`\n"
        f"**Commit:** `{commit_sha}`\n\n"
        f"### Acceptance criteria addressed\n\n"
        f"{ac_list}\n\n"
        f"---\n\n"
        f"*Generated by dev-pipeline-v1. "
        f"Do not edit files under `src/` or `tests/` directly — "
        f"re-run the pipeline to apply changes.*\n"
    )
