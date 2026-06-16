"""
agents/commit_writer.py — Mechanical codeforge commit agent.

Two operations, no LLM:
  commit_codeforge_state — git add + commit + push the project-state/ directory after the
                           state_writer has already flushed pending_writes to disk.
  commit_source_code     — write src/, tests/, requirements*.txt to the source code repo,
                           create a branch, commit, push, open a GitHub PR.

CommitWriter does NOT increment agent_call_count — the orchestrator skips the ceiling
check for CommitWriter invocations.

Constructor:
    config      — ConfigSnapshot (provides repos config and github_token)
    project_dir — Path to the managed project directory (the codeforge state git repo root)

CommitWriterInput.codeforge_state and .source_code are untyped dicts to avoid a circular
dependency between contracts and the agent layer. Expected shapes:

  codeforge_state: (ignored — files are already on disk when commit_codeforge_state is called)

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

    def commit_codeforge_state(self, input: CommitWriterInput) -> CommitWriterResult:
        """
        Commit the project-state/ directory to the codeforge state repo and push.

        Precondition: state_writer.flush_pending_writes() has already written all
        changed JSON + markdown files to disk. This method only does the git work.
        """
        try:
            repo = git.Repo(self._project_dir)

            # Stage only the project-state/ subtree
            repo.git.add("project-state/")

            # Nothing to commit is not an error — maybe this run produced no state changes.
            # On a brand-new repo with no initial commit, HEAD is unborn and cannot be
            # diffed against (GitPython raises BadName), so only run the diff-against-HEAD
            # short-circuit when HEAD already resolves to a commit. With an unborn HEAD we
            # fall through and let index.commit create the initial commit.
            if repo.head.is_valid() and not repo.index.diff("HEAD") and not repo.untracked_files:
                commit_sha = repo.head.commit.hexsha
                return CommitWriterResult(
                    target="codeforge_state",
                    success=True,
                    commit_sha=commit_sha,
                )

            message = (
                f"chore(codeforge): run {input.run_id} — {input.feature_title}"
            )
            commit = repo.index.commit(message)

            repos_cfg = self._config.repos
            if repos_cfg and repos_cfg.codeforge_state.remote:
                remote = _get_or_add_remote(
                    repo,
                    repos_cfg.codeforge_state.remote,
                    "codeforge_state",
                )
                branch = repos_cfg.codeforge_state.branch
                repo.git.push(remote, f"HEAD:{branch}")

            return CommitWriterResult(
                target="codeforge_state",
                success=True,
                commit_sha=commit.hexsha,
            )

        except Exception as exc:
            return CommitWriterResult(
                target="codeforge_state",
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

            branch_name = f"{src_cfg.branch_prefix}{input.run_id}"

            # Start from a clean default branch when the repo already has history.
            # On a brand-new repo with no commits, HEAD is unborn: there is no
            # default-branch commit to base off (`git checkout <default>` errors with
            # "pathspec did not match"), so we branch directly off the unborn HEAD and
            # the commit below becomes the repo's initial commit.
            if repo.head.is_valid():
                repo.git.checkout(src_cfg.default_branch)

            # Create the feature branch, or reuse it on a resume/retry where a prior
            # attempt already created it (`checkout -b` errors if it exists).
            if branch_name in (h.name for h in repo.heads):
                repo.git.checkout(branch_name)
            else:
                repo.git.checkout("-b", branch_name)

            # Write source files
            _write_code_artifact(source_repo_path, code_artifact)
            _write_test_suite(source_repo_path, test_suite)

            # Commit
            repo.git.add("-A")
            message = f"feat({input.feature_title}): implement {input.feature_title}"
            commit = repo.index.commit(message)
            commit_sha = commit.hexsha

            # Push + open PR only when a remote is configured. A local-only project
            # (empty remote) commits to the local branch and stops there — mirrors the
            # remote guard in commit_codeforge_state.
            pr_url: str | None = None
            if src_cfg.remote:
                remote = _get_or_add_remote(repo, src_cfg.remote, "origin")
                repo.git.push(remote, f"{branch_name}:{branch_name}", "--set-upstream")
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

def _write_code_artifact(repo_root: Path, code_artifact: CodeArtifact) -> None:
    # Paths are project-root-relative (src/foo.py, requirements.txt, ...) and are
    # written verbatim — matching how the test runner stages files. Do NOT re-prefix
    # with an output dir or the files land at src/src/foo.py.
    for f in code_artifact.files:
        target = repo_root / f.path
        if f.change_type == "deleted":
            if target.exists():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.content, encoding="utf-8")


def _write_test_suite(repo_root: Path, test_suite: TestSuite) -> None:
    # Test paths are project-root-relative too (tests/test_foo.py, conftest.py,
    # requirements-test.txt) — written verbatim, no tests/ re-prefix.
    for test_case in test_suite.test_cases:
        for f in test_case.code:
            target = repo_root / f.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f.content, encoding="utf-8")

    for f in test_suite.test_infrastructure:
        target = repo_root / f.path
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
        name = f"{preferred_name}_codeforge"
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
        f"## Codeforge-generated feature\n\n"
        f"**Feature:** {input.feature_title}\n"
        f"**Codeforge run:** `{input.run_id}`\n"
        f"**Codeforge version:** `{input.codeforge_version}`\n"
        f"**Commit:** `{commit_sha}`\n\n"
        f"### Acceptance criteria addressed\n\n"
        f"{ac_list}\n\n"
        f"---\n\n"
        f"*Generated by codeforge-v1. "
        f"Do not edit files under `src/` or `tests/` directly — "
        f"re-run codeforge to apply changes.*\n"
    )
