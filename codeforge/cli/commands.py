"""
cli/commands.py — Typer CLI for codeforge-v1.

Three commands:
  init    — scaffold .codeforge/ in a project directory
  run     — execute a codeforge run end-to-end
  resume  — resume a codeforge run that was interrupted by an escalation

The CLI is a thin wrapper: it owns the lock, loads config, creates the state machine,
drives execute(), and hands off to CommitWriter. All phase logic lives in the state machine.
"""

from __future__ import annotations

import json
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import typer


class RunMode(str, Enum):
    """Run modes for `codeforge run`. Kept in sync with the run_mode Literal in
    schemas/contracts.py."""

    new_project = "new_project"
    continuation = "continuation"

from codeforge.cli.interaction import HumanInteraction
from codeforge.cli.lock import CodeforgeAlreadyRunningError, CodeforgeLock
from codeforge.config.config_loader import load_config
from codeforge.orchestrator.state_machine import EscalationError, StateMachine
from codeforge.schemas.contracts import CodeforgeRun, CommitWriterInput, EscalationEvent

app = typer.Typer(
    name="codeforge",
    help="codeforge — AI-driven software development tool.",
    add_completion=False,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CODEFORGE_CONFIG_TEMPLATE = """\
# Project-local codeforge configuration
# Fill in the required fields before running `codeforge run`.
#
# Merge rules: all keys here override the codeforge installation defaults.
# You only need to set fields that differ from the defaults.

# Target tech stack. "python" (default) or "nextjs-supabase". The profile supplies a default
# sandbox image when test_runner.sandbox_image is left blank.
stack:
  profile: "python"

repos:
  codeforge_state:
    remote: ""          # git remote URL for this codeforge state repo
    branch: "main"

  source_code:
    path: ""            # absolute path to the source code repo on this machine
    remote: ""          # git remote URL (used to open PRs)
    default_branch: "main"
    branch_prefix: "codeforge/"
    pr_target: "main"
    auto_merge: false   # local main is canonical (accumulates by fast-forward); the PR
                        # is for review. true auto-squash-merges on the remote (diverges).
    output_dir: "src"

test_runner:
  sandbox_image: ""     # Docker image tag for the test sandbox
  timeout_seconds: 300
  environment_vars: {}
"""


def _run_log_dir(project_dir: Path) -> Path:
    return project_dir / "run-logs"


def _load_brief(run_log_dir: Path, run_id: str) -> str | None:
    brief_file = run_log_dir / run_id / "brief.txt"
    if brief_file.exists():
        return brief_file.read_text(encoding="utf-8").strip()
    return None


def _save_brief(run_log_dir: Path, run_id: str, brief: str) -> None:
    dest = run_log_dir / run_id
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "brief.txt").write_text(brief, encoding="utf-8")


def _load_codeforge_run(run_log_dir: Path, run_id: str) -> dict[str, Any]:
    run_file = run_log_dir / run_id / "codeforge_run.json"
    if not run_file.exists():
        typer.echo(f"No codeforge_run.json found for run '{run_id}' in {run_log_dir}", err=True)
        raise typer.Exit(1)
    result: dict[str, Any] = json.loads(run_file.read_text(encoding="utf-8"))
    return result


def _select_resume_escalation(
    run: "CodeforgeRun",
) -> "tuple[EscalationEvent | None, bool]":
    """Pick the escalation that resume should act on, and whether to prompt.

    Returns (escalation, needs_prompt):
      - latest escalation unresolved -> (escalation, True)   # prompt the operator
      - latest escalation resolved   -> (escalation, False)  # silent re-entry
      - no escalations               -> (None, False)        # default reentry

    The *latest* escalation is the relevant one whether or not it is resolved: when
    a prior resume crashed after recording the resolution, the escalation is left
    resolved on disk and we re-enter silently using its stored reentry_directive.
    A rejected escalation is never marked resolved (rejection aborts before that),
    so it remains unresolved and re-prompts on the next resume.
    """
    if not run.escalations:
        return None, False
    latest = run.escalations[-1]
    return latest, (not latest.resolved)


def _initial_state_from(escalation: "EscalationEvent | None") -> str:
    """Reentry state for resume: the escalation's reentry_directive, else requirements."""
    if escalation and escalation.resolution and escalation.resolution.reentry_directive:
        return escalation.resolution.reentry_directive.reentry_state
    return "requirements"


def _do_commit(
    sm: StateMachine,
    req_doc: Any,
    code_art: Any,
    test_suite: Any,
    config: Any,
    project_dir: Path,
) -> None:
    from codeforge.agents.commit_writer import CommitWriter
    from codeforge.orchestrator.routing import route_commit_state_fail, route_commit_src_fail, route_commit_success

    run = sm.run
    writer = CommitWriter(config, project_dir, run_log_dir=_run_log_dir(project_dir))
    state_commit_sha: str | None = None

    # Commit codeforge state — retry up to codeforge_state_commit limit
    while True:
        state_result = writer.commit_codeforge_state(
            CommitWriterInput(
                target="codeforge_state",
                run_id=run.run_id,
                codeforge_version=config.name,
                feature_title=req_doc.feature_title,
                ac_ids=[ac.id for ac in req_doc.acceptance_criteria],
            )
        )
        if state_result.success:
            state_commit_sha = state_result.commit_sha
            typer.echo(f"Codeforge state committed: {state_commit_sha}")
            break

        outcome = route_commit_state_fail(run.retry_counters, config.to_dict())
        sm._apply_outcome(outcome)  # increments counter + emits routing event
        if outcome.decision == "escalate":
            # Record the escalation (status=failed_escalated, suggested_reentry_state
            # =commit) so the run stays resumable, then raise EscalationError.
            sm._escalate(
                "commit_failure",
                f"State commit failed after retries: {state_result.error_message}",
            )
        typer.echo(
            f"State commit failed, retrying ({state_result.error_message})", err=True
        )

    # Commit source code — retry up to source_code_commit limit
    while True:
        src_result = writer.commit_source_code(
            CommitWriterInput(
                target="source_code",
                run_id=run.run_id,
                codeforge_version=config.name,
                feature_title=req_doc.feature_title,
                ac_ids=[ac.id for ac in req_doc.acceptance_criteria],
                source_code={
                    "code_artifact": code_art.model_dump(),
                    "test_suite": test_suite.model_dump(),
                    "codeforge_state_commit_sha": state_commit_sha,
                },
            )
        )
        if src_result.success:
            typer.echo(f"Source code committed: {src_result.commit_sha}")
            if src_result.pr_url:
                typer.echo(f"PR opened: {src_result.pr_url}")
            break

        outcome = route_commit_src_fail(run.retry_counters, config.to_dict())
        sm._apply_outcome(outcome)
        if outcome.decision == "escalate":
            sm._escalate(
                "commit_failure",
                f"Source commit failed after retries: {src_result.error_message}",
            )
        typer.echo(
            f"Source commit failed, retrying ({src_result.error_message})", err=True
        )

    sm._apply_outcome(route_commit_success())

    # The git commit has now landed — promote the run to succeeded. run_commit()
    # deliberately leaves status untouched so a commit failure stays resumable.
    run.status = "succeeded"
    sm.event_log.update_run_snapshot(run)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.command()
def init(
    project_dir: Path = typer.Option(
        ...,
        "--project-dir",
        "-d",
        help="Path to the project directory to initialise.",
        show_default=False,
    ),
) -> None:
    """
    Scaffold .codeforge/ configuration for a new managed project.

    Creates .codeforge/codeforge.config.yaml with a template the operator
    must fill in before running `codeforge run`.
    """
    codeforge_dir = project_dir / ".codeforge"
    config_path = codeforge_dir / "codeforge.config.yaml"

    if codeforge_dir.exists():
        typer.echo(
            f"Error: {codeforge_dir} already exists. "
            "Delete it to reinitialise, or edit it directly.",
            err=True,
        )
        raise typer.Exit(1)

    codeforge_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_CODEFORGE_CONFIG_TEMPLATE, encoding="utf-8")

    typer.echo(f"Initialised codeforge configuration at {config_path}")
    typer.echo("Edit the file and fill in 'repos' and 'test_runner.sandbox_image' before running.")


@app.command()
def run(
    project_dir: Path = typer.Option(
        ...,
        "--project-dir",
        "-d",
        help="Path to the managed project directory (contains .codeforge/).",
        show_default=False,
    ),
    brief: str = typer.Option(
        ...,
        "--brief",
        "-b",
        help="One-sentence feature brief passed to the requirements analyst.",
        show_default=False,
    ),
    run_mode: RunMode = typer.Option(
        RunMode.new_project,
        "--run-mode",
        help=(
            "Pipeline mode. "
            "'new_project': build a project from scratch (the brief describes the "
            "whole project). "
            "'continuation': add a feature to the existing codebase — the "
            "architecture designer, coder, and reviewers get read-only tools to "
            "search and read the current source, and edits are applied as surgical "
            "diffs (the brief describes the feature to add)."
        ),
        case_sensitive=False,
    ),
) -> None:
    """
    Execute a full codeforge run for the given project and brief.

    Acquires a per-project lock, drives all seven phases, and
    commits both codeforge state and source code on success.

    Run modes:
      new_project   Greenfield build; agents design and implement from the brief alone.
      continuation  Add a feature to an existing repo; tool-enabled agents read the
                    current code and emit diff-based edits. Requires the repos block
                    in .codeforge/codeforge.config.yaml.
    """
    config = _load_config_or_exit(project_dir)
    lock = CodeforgeLock(project_dir)
    human = HumanInteraction()

    try:
        lock.acquire()
    except CodeforgeAlreadyRunningError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    run_log_dir = _run_log_dir(project_dir)
    sm = StateMachine(config, project_dir, run_log_dir)

    try:
        codeforge_run = sm.start_run(run_mode.value, brief)
        _save_brief(run_log_dir, codeforge_run.run_id, brief)
        typer.echo(f"Run started: {codeforge_run.run_id}")

        req_doc, code_art, test_suite = sm.execute(brief, human)
        typer.echo(f"Codeforge succeeded (run {codeforge_run.run_id})")

        _do_commit(sm, req_doc, code_art, test_suite, config, project_dir)

    except EscalationError as exc:
        run_id = sm.run.run_id if sm._run else "unknown"
        typer.echo(f"\nCodeforge escalated: {exc.reason}", err=True)
        typer.echo(f"Run ID: {run_id}", err=True)
        typer.echo(
            f"Review run-logs/{run_id}/events.jsonl for details.",
            err=True,
        )
        raise typer.Exit(2)

    except Exception as exc:
        sm.mark_failed_terminal()
        typer.echo(f"Codeforge failed with unexpected error: {exc}", err=True)
        raise typer.Exit(1)

    finally:
        lock.release()


@app.command()
def resume(
    run_id: str = typer.Argument(help="Run ID to resume (e.g. run-abc123)."),
    project_dir: Path = typer.Option(
        ...,
        "--project-dir",
        "-d",
        help="Path to the managed project directory.",
        show_default=False,
    ),
) -> None:
    """
    Resume a codeforge run that was interrupted by an escalation.

    Loads the saved CodeforgeRun, presents the pending escalation event to
    the operator, collects their EscalationResolution, and re-enters the
    state machine from the chosen reentry state.
    """
    config = _load_config_or_exit(project_dir)
    lock = CodeforgeLock(project_dir)
    human = HumanInteraction()

    run_log_dir = _run_log_dir(project_dir)
    run_data = _load_codeforge_run(run_log_dir, run_id)

    try:
        lock.acquire()
    except CodeforgeAlreadyRunningError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)

    sm = StateMachine(config, project_dir, run_log_dir)

    try:
        codeforge_run = CodeforgeRun(**run_data)

        # Nothing to resume on a completed run.
        if codeforge_run.status == "succeeded":
            typer.echo(f"Run {run_id} already succeeded — nothing to resume.")
            raise typer.Exit(0)

        # Pick the escalation to act on. Prompt only for a still-unresolved one;
        # an already-resolved escalation (e.g. a prior resume that crashed after
        # recording the resolution) is re-entered silently using its stored
        # reentry_directive.
        escalation, needs_prompt = _select_resume_escalation(codeforge_run)
        freshly_resolved = False
        if escalation is not None and needs_prompt:
            typer.echo(f"\nResuming run {run_id} — pending escalation:")
            resolution = human.handle_escalation(escalation)

            if resolution.outcome == "rejected":
                typer.echo("Escalation rejected — aborting run.", err=True)
                raise typer.Exit(2)

            from datetime import datetime, timezone
            escalation.resolved = True
            escalation.resolution = resolution
            escalation.resolved_at = datetime.now(timezone.utc).isoformat()
            freshly_resolved = True
        elif escalation is not None:
            typer.echo(
                f"Re-entering run {run_id} at previously resolved escalation "
                f"(reentry state: {_initial_state_from(escalation)})."
            )
        else:
            typer.echo(f"Resuming run {run_id} (no recorded escalation).")

        sm.resume_run(codeforge_run)

        # Write human_override entry only when the operator just modified the run.
        # Gated on freshly_resolved so re-resumes don't duplicate the entry.
        if (
            freshly_resolved
            and escalation is not None
            and escalation.resolution
            and escalation.resolution.outcome == "modified"
        ):
            import uuid as _uuid
            from datetime import datetime, timezone
            sm.pending.merge_append("decisions_log", [{
                "entry_id": str(_uuid.uuid4()),
                "run_id": run_id,
                "entry_type": "human_override",
                "source_agent": None,
                "decision": escalation.resolution.change_summary or "",
                "rationale": escalation.resolution.human_notes,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }])

        # Apply the resolution's reentry directive (if any). This runs for both a
        # freshly-resolved and a previously-resolved escalation so re-resumes land
        # at the same reentry state. counter_resets reset to 0 (idempotent); a
        # reset_global_ceiling re-zeroes agent_call_count, which is acceptable
        # because this path only runs when a prior resume did not complete.
        initial_state = "requirements"
        if escalation and escalation.resolution and escalation.resolution.reentry_directive:
            directive = escalation.resolution.reentry_directive
            initial_state = directive.reentry_state
            from codeforge.orchestrator.routing import RoutingOutcome as _RO
            reset_outcome = _RO(
                row_id="X-resume",
                decision="retry_same_agent",
                next_state=initial_state,
                counter_resets=directive.counter_resets,
            )
            sm._apply_outcome(reset_outcome)
            if directive.reset_global_ceiling:
                sm.run.agent_call_count = 0

        brief = _load_brief(run_log_dir, run_id)
        if brief is None:
            brief = typer.prompt("Brief not found in run logs — please re-enter the brief")

        typer.echo(f"Resuming from state: {initial_state}")
        req_doc, code_art, test_suite = sm.execute(brief, human, initial_state=initial_state)
        typer.echo(f"Codeforge succeeded (run {run_id})")

        _do_commit(sm, req_doc, code_art, test_suite, config, project_dir)

    except typer.Exit:
        # Deliberate exits (rejection, succeeded guard) must propagate with their
        # own code and must not be treated as a terminal failure.
        raise

    except EscalationError as exc:
        typer.echo(f"\nCodeforge escalated again: {exc.reason}", err=True)
        typer.echo(f"Review run-logs/{run_id}/events.jsonl for details.", err=True)
        raise typer.Exit(2)

    except Exception as exc:
        sm.mark_failed_terminal()
        typer.echo(f"Codeforge failed: {exc}", err=True)
        raise typer.Exit(1)

    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Config loading helper
# ---------------------------------------------------------------------------

def _load_config_or_exit(project_dir: Path) -> Any:
    """Load config with full validation; exit with a friendly message on failure."""
    try:
        return load_config(
            project_dir,
            require_sandbox_image=True,
            require_repos=True,
            require_env_vars=True,
        )
    except (ValueError, EnvironmentError, FileNotFoundError) as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(1)
