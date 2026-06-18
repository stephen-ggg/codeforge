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
import shutil
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
from codeforge.store.edits import EditError, apply_edits


class CommitWriter:
    def __init__(
        self,
        config: ConfigSnapshot,
        project_dir: Path,
        run_log_dir: Path | None = None,
    ) -> None:
        self._config = config
        self._project_dir = Path(project_dir)
        # Per-run worktrees are created under run-logs/<run_id>/ (gitignored in the
        # state repo, namespaced per run). Defaults off project_dir for callers that
        # don't thread it explicitly; the CLI passes the real run-logs path.
        self._run_log_dir = (
            Path(run_log_dir) if run_log_dir is not None else self._project_dir / "run-logs"
        )

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
            # The state repo is bootstrapped with an initial commit at project creation, so
            # HEAD always resolves and the diff-against-HEAD short-circuit is safe.
            if not repo.index.diff("HEAD") and not repo.untracked_files:
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
        Build the feature commit in an isolated per-run worktree off an immutable base
        (the current tip of the default branch), push it + open a PR, then fast-forward
        the canonical default branch to include it. The canonical checkout is never moved
        onto the feature branch and is never written to mid-run, so a failure anywhere
        before the fast-forward leaves it pristine and resumable.
        """
        worktree_dir: Path | None = None
        repo: git.Repo | None = None
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
            default_branch = src_cfg.default_branch
            branch_name = f"{src_cfg.branch_prefix}{input.run_id}"

            # The default branch is bootstrapped at project creation. Its absence means
            # the project was not set up correctly — fail loud rather than minting it
            # from an arbitrary HEAD (which would bless stale/wrong state as the base).
            if not repo.head.is_valid() or default_branch not in (h.name for h in repo.heads):
                raise ValueError(
                    f"source repo has no '{default_branch}' branch — project was not "
                    f"bootstrapped correctly (expected an initial commit on "
                    f"'{default_branch}'). Create it and re-run."
                )

            # The canonical checkout must rest on the default branch between runs; the
            # fast-forward below advances *that* branch, so refuse to operate from any
            # other branch rather than silently advancing the wrong ref.
            if repo.active_branch.name != default_branch:
                raise ValueError(
                    f"source repo is on '{repo.active_branch.name}', expected "
                    f"'{default_branch}' — leftover state from a prior run; reset to "
                    f"'{default_branch}' and re-run."
                )

            # The fast-forward below updates the canonical working tree in place, so it
            # must be clean: git aborts a merge that would overwrite locally-modified
            # tracked files, and that abort is not self-healing — retrying escalates with
            # an opaque "local changes would be overwritten" error. Fail fast and actionable
            # instead, naming the offending files (untracked files don't block a merge).
            if repo.is_dirty(untracked_files=False):
                dirty = ", ".join(sorted(d.a_path for d in repo.index.diff(None))) or "(staged changes)"
                raise ValueError(
                    f"source repo has uncommitted local changes ({dirty}) — the canonical "
                    f"checkout must be clean between runs so the fast-forward can advance it. "
                    f"Commit, stash, or discard them and re-run."
                )

            base_sha = repo.commit(default_branch).hexsha  # explicit, immutable base

            # Build the feature commit in a throwaway worktree off the immutable base.
            # Idempotent across resume/retry: clear any stale worktree, reuse the branch
            # if a prior attempt already created it.
            worktree_dir = self._run_log_dir / input.run_id / "source-worktree"
            worktree_dir.parent.mkdir(parents=True, exist_ok=True)
            _prune_worktree(repo, worktree_dir)
            if branch_name in (h.name for h in repo.heads):
                repo.git.worktree("add", str(worktree_dir), branch_name)
            else:
                repo.git.worktree("add", str(worktree_dir), "-b", branch_name, base_sha)

            wt = git.Repo(worktree_dir)
            _write_code_artifact(worktree_dir, code_artifact)
            _write_test_suite(worktree_dir, test_suite)

            wt.git.add("-A")
            message = f"feat({input.feature_title}): implement {input.feature_title}"
            # Skip the commit when nothing changed (resume after the commit already
            # landed but a later step failed); reuse the existing branch tip.
            if wt.git.diff("--cached", "--name-only").strip():
                wt.git.commit("-m", message)
            commit_sha = wt.head.commit.hexsha

            # Push + open PR only when a remote is configured. Do this BEFORE advancing
            # the local default branch so a push failure escalates with it left pristine.
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

            # Advance the canonical default branch to include the feature. The base has
            # not moved (writes went to the worktree), so this fast-forward is exact and
            # conflict-free; it updates the canonical working tree in place.
            repo.git.merge(branch_name, "--ff-only")

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
        finally:
            # Tear down the worktree (keeps the branch ref for the PR). The canonical
            # checkout is untouched on any pre-merge failure, so the run stays resumable.
            if repo is not None and worktree_dir is not None:
                _prune_worktree(repo, worktree_dir)


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
        elif f.change_type == "modified" and f.edits:
            # Continuation: apply surgical edits against the existing file so
            # unrelated code is never clobbered. Fail loudly on no/ambiguous match.
            if not target.exists():
                raise EditError(f"cannot apply edits to missing file: {f.path}")
            original = target.read_text(encoding="utf-8")
            target.write_text(apply_edits(original, f.edits), encoding="utf-8")
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

def _prune_worktree(repo: git.Repo, worktree_dir: Path) -> None:
    """Deregister and delete a per-run worktree, tolerating a missing/stale one.

    Used both to clear stale state before `worktree add` and to tear down afterwards.
    The associated branch ref is preserved (worktree removal does not delete it).
    """
    try:
        repo.git.worktree("remove", "--force", str(worktree_dir))
    except git.exc.GitCommandError:
        pass  # not a registered worktree (never created, or already removed)
    try:
        repo.git.worktree("prune")
    except git.exc.GitCommandError:
        pass
    # A leftover plain directory (not a registered worktree) won't be touched above.
    if worktree_dir.exists():
        shutil.rmtree(worktree_dir, ignore_errors=True)


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
