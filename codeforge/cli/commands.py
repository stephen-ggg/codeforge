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
from typing import Any, Optional, cast

import typer


class RunMode(str, Enum):
    """Run modes for `codeforge run`. Kept in sync with the run_mode Literal in
    schemas/contracts.py."""

    new_project = "new_project"
    continuation = "continuation"

from codeforge.cli.interaction import HumanInteraction, reentry_options_for
from codeforge.cli.lock import CodeforgeAlreadyRunningError, CodeforgeLock
from codeforge.config.config_loader import load_config
from codeforge.orchestrator.state_machine import (
    ConfigChangedError,
    EscalationError,
    SchemaVersionMismatchError,
    StateMachine,
)
from codeforge.schemas.contracts import (
    CodeforgeRun,
    CommitWriterInput,
    EscalationEvent,
    EscalationResolution,
    ReentryDirective,
    ReentryState,
)

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

# Target tech stack. "python" (default), "nextjs", or "nextjs-supabase". The profile
# supplies a default sandbox image when test_runner.sandbox_image is left blank.
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


def _confirm_config_change(changes: list[dict[str, Any]]) -> bool:
    """Interactive callback: show the config diff and ask the operator to confirm.

    Used only on the interactive resume path; programmatic callers pass
    --allow-config-change instead.
    """
    typer.echo("\nThe config has changed since this run started:")
    for change in changes:
        typer.echo(f"  {change['path']}: {change['old']!r} -> {change['new']!r}")
    return typer.confirm("Resume under the new config? (the change will be recorded)")


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
# list-runs helper
# ---------------------------------------------------------------------------

def _read_run_summary(run_log_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Read a single run's summary from disk. Returns None if the run is unreadable."""
    run_dir = run_log_dir / run_id
    run_file = run_dir / "codeforge_run.json"
    if not run_file.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(run_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    brief_file = run_dir / "brief.txt"
    brief = brief_file.read_text(encoding="utf-8").strip() if brief_file.exists() else None

    # Surface the reason and phase for the latest unresolved escalation, if any.
    escalation_reason: str | None = None
    failed_phase: str | None = None
    escalations = data.get("escalations") or []
    if escalations:
        latest = escalations[-1]
        if not latest.get("resolved"):
            escalation_reason = latest.get("reason")
            failed_phase = latest.get("suggested_reentry_state")

    return {
        "run_id": data.get("run_id", run_id),
        "status": data.get("status"),
        "started_at": data.get("started_at"),
        "run_mode": data.get("run_mode"),
        "brief": brief,
        "escalation_reason": escalation_reason,
        "failed_phase": failed_phase,
        "agent_call_count": data.get("agent_call_count"),
    }


# ---------------------------------------------------------------------------
# Non-interactive resume helper
# ---------------------------------------------------------------------------

def _build_noninteractive_resolution(
    decision: str,
    reentry_state: Optional[str],
    reset_counters: Optional[str],
    instructions: Optional[str],
    notes: Optional[str],
    suggested_reentry: Optional[str],
    reason: str,
) -> "EscalationResolution":
    """Build an EscalationResolution from CLI flags without prompting."""
    decision = decision.lower().strip()
    if decision not in ("approve", "reject", "modify"):
        typer.echo(f"--decision must be approve, reject, or modify (got: {decision!r})", err=True)
        raise typer.Exit(1)

    if decision == "reject":
        return EscalationResolution(outcome="rejected", human_notes=notes or "")

    state = reentry_state or suggested_reentry
    if not state:
        typer.echo(
            "--reentry-state is required for approve/modify "
            "(no suggested state found in escalation).",
            err=True,
        )
        raise typer.Exit(1)

    # Reject an explicit reentry state downstream of the failing phase (or not allowed
    # for this reason) — mirrors the bounded interactive menu so a run that died in
    # test_design can't be resumed straight into commit on artifacts that don't exist.
    if reentry_state is not None:
        valid = reentry_options_for(reason, suggested_reentry)
        if valid and reentry_state not in valid:
            typer.echo(
                f"--reentry-state {reentry_state!r} is not valid for a {reason} "
                f"escalation that failed at {suggested_reentry!r}. "
                f"Valid options: {', '.join(valid)}.",
                err=True,
            )
            raise typer.Exit(1)

    counter_list: list[str] = []
    if reset_counters:
        counter_list = [c.strip() for c in reset_counters.split(",") if c.strip()]

    directive = ReentryDirective(
        reentry_state=cast(ReentryState, state),
        counter_resets=counter_list,
        reset_global_ceiling=False,
    )

    if decision == "modify":
        if not instructions:
            typer.echo("--instructions is required for --decision modify.", err=True)
            raise typer.Exit(1)
        return EscalationResolution(
            outcome="modified",
            change_summary=instructions,
            reentry_directive=directive,
            human_notes=notes or "",
        )

    return EscalationResolution(
        outcome="approved",
        reentry_directive=directive,
        human_notes=notes or "",
    )


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
    brief: Optional[str] = typer.Option(
        None,
        "--brief",
        "-b",
        help="Feature brief passed to the requirements analyst (inline string).",
        show_default=False,
    ),
    brief_file: Optional[Path] = typer.Option(
        None,
        "--brief-file",
        help="Path to a file whose contents are used as the feature brief. Mutually exclusive with --brief.",
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
    if brief is not None and brief_file is not None:
        typer.echo("Error: --brief and --brief-file are mutually exclusive.", err=True)
        raise typer.Exit(1)
    if brief is None and brief_file is None:
        typer.echo("Error: one of --brief or --brief-file is required.", err=True)
        raise typer.Exit(1)

    if brief_file is not None:
        if not brief_file.exists():
            typer.echo(f"Error: brief file not found: {brief_file}", err=True)
            raise typer.Exit(1)
        resolved_brief = brief_file.read_text(encoding="utf-8").strip()
        if not resolved_brief:
            typer.echo(f"Error: brief file is empty: {brief_file}", err=True)
            raise typer.Exit(1)
    else:
        resolved_brief = brief.strip()  # type: ignore[assignment]
        if not resolved_brief:
            typer.echo("Error: --brief value is empty.", err=True)
            raise typer.Exit(1)

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
        codeforge_run = sm.start_run(run_mode.value, resolved_brief)
        _save_brief(run_log_dir, codeforge_run.run_id, resolved_brief)
        typer.echo(f"Run started: {codeforge_run.run_id}")

        req_doc, code_art, test_suite = sm.execute(resolved_brief, human)
        typer.echo(f"Codeforge succeeded (run {codeforge_run.run_id})")

        _do_commit(sm, req_doc, code_art, test_suite, config, project_dir)

    except EscalationError as exc:
        run_id = sm.run.run_id if sm._run else "unknown"
        typer.echo(f"\nCodeforge escalated: {exc.reason}", err=True)
        if exc.phase:
            typer.echo(f"Phase: {exc.phase}", err=True)
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
    decision: Optional[str] = typer.Option(
        None,
        "--decision",
        help="Non-interactive: approve, reject, or modify. Skips all prompts.",
        show_default=False,
    ),
    reentry_state: Optional[str] = typer.Option(
        None,
        "--reentry-state",
        help="Non-interactive: reentry state name (required for approve/modify).",
        show_default=False,
    ),
    reset_counters: Optional[str] = typer.Option(
        None,
        "--reset-counters",
        help="Non-interactive: comma-separated retry counter names to reset to zero.",
        show_default=False,
    ),
    instructions: Optional[str] = typer.Option(
        None,
        "--instructions",
        help="Non-interactive (modify only): description of the change being made.",
        show_default=False,
    ),
    notes: Optional[str] = typer.Option(
        None,
        "--notes",
        help="Non-interactive: human notes attached to the resolution.",
        show_default=False,
    ),
    allow_config_change: bool = typer.Option(
        False,
        "--allow-config-change",
        help=(
            "Permit resuming under a config that differs from the run's persisted "
            "snapshot. The change is recorded to the run's audit trail. A schema_version "
            "change is never permitted (start a new run instead)."
        ),
    ),
) -> None:
    """
    Resume a codeforge run that was interrupted by an escalation.

    Loads the saved CodeforgeRun, presents the pending escalation event to
    the operator, collects their EscalationResolution, and re-enters the
    state machine from the chosen reentry state.

    Pass --decision to skip all interactive prompts (for programmatic / web use):
      --decision approve --reentry-state <state>
      --decision reject
      --decision modify --reentry-state <state> --instructions "change X to Y"
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
            if decision is not None:
                resolution = _build_noninteractive_resolution(
                    decision=decision,
                    reentry_state=reentry_state,
                    reset_counters=reset_counters,
                    instructions=instructions,
                    notes=notes,
                    suggested_reentry=getattr(escalation, "suggested_reentry_state", None),
                    reason=escalation.reason,
                )
            else:
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

        # Reconcile the run's persisted config snapshot against the current on-disk
        # config: hard-block on a schema_version change, and require explicit opt-in
        # (flag or interactive confirm) for any other drift. Prompt only in
        # interactive mode (no --decision); programmatic callers must pass the flag.
        confirm_cb = None if decision is not None else _confirm_config_change
        sm.reconcile_config_on_resume(
            allow_config_change=allow_config_change,
            interactive_confirm=confirm_cb,
        )

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

    except SchemaVersionMismatchError as exc:
        typer.echo(
            f"\nCannot resume: {exc}.\n"
            "A schema_version change can make persisted artifacts incompatible. "
            "Start a new run instead.",
            err=True,
        )
        raise typer.Exit(2)

    except ConfigChangedError as exc:
        typer.echo(
            "\nCannot resume: the config has changed since this run started.",
            err=True,
        )
        for change in exc.changes:
            typer.echo(
                f"  {change['path']}: {change['old']!r} -> {change['new']!r}", err=True
            )
        typer.echo(
            "\nRe-run with --allow-config-change to resume under the new config "
            "(the change will be recorded to the run's audit trail).",
            err=True,
        )
        raise typer.Exit(1)

    except EscalationError as exc:
        typer.echo(f"\nCodeforge escalated again: {exc.reason}", err=True)
        if exc.phase:
            typer.echo(f"Phase: {exc.phase}", err=True)
        typer.echo(f"Review run-logs/{run_id}/events.jsonl for details.", err=True)
        raise typer.Exit(2)

    except Exception as exc:
        sm.mark_failed_terminal()
        typer.echo(f"Codeforge failed: {exc}", err=True)
        raise typer.Exit(1)

    finally:
        lock.release()


@app.command()
def seed(
    project_dir: Path = typer.Option(
        ...,
        "--project-dir",
        "-d",
        help="Path to the managed project directory.",
        show_default=False,
    ),
    ui_design: Path = typer.Option(
        ...,
        "--ui-design",
        help="Path to the .dc.html design file to seed from.",
        show_default=False,
    ),
) -> None:
    """
    One-time bootstrap of the ui_design project state document from a .dc.html file.

    Parses the design file, writes a draft ui_design.json and ui_design.md to
    project-state/, and prints a summary. The output MUST be reviewed and edited
    before committing — values not extractable automatically are scaffolded as
    'TODO: fill in' placeholders.
    """
    from codeforge.cli.seed_parser import SeedParser
    from codeforge.store.project_state import ProjectStateStore

    if not ui_design.exists():
        typer.echo(f"Error: file not found: {ui_design}", err=True)
        raise typer.Exit(1)

    if not project_dir.exists():
        typer.echo(f"Error: project directory not found: {project_dir}", err=True)
        raise typer.Exit(1)

    state_dir = project_dir / "project-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    existing = state_dir / "ui_design.json"

    # Atomically claim the output file with exclusive-create mode so concurrent
    # invocations cannot both pass the "already seeded" guard.
    try:
        existing.touch(exist_ok=False)
    except FileExistsError:
        typer.echo(
            "Error: ui_design already seeded. "
            f"Edit {existing} directly to make changes.",
            err=True,
        )
        raise typer.Exit(1)

    try:
        ui_state = SeedParser(ui_design).parse()
    except ValueError as exc:
        existing.unlink(missing_ok=True)  # release the placeholder on parse failure
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    store = ProjectStateStore(project_dir)
    store.write("ui_design", ui_state.model_dump())

    typer.echo(f"Seeded ui_design from: {ui_design.name}")
    typer.echo(f"  Design tokens extracted: {len(ui_state.design_tokens)}")
    typer.echo(f"  Phase colors extracted:  {len(ui_state.phase_colors)}")
    typer.echo(f"  Components scaffolded:   {len(ui_state.components)}")
    typer.echo(f"  Font family:             {ui_state.font_family}")
    typer.echo("")
    typer.echo("⚠  REVIEW REQUIRED before committing.")
    typer.echo(f"   Edit {existing} — search for 'TODO: fill in' placeholders.")
    typer.echo(f"   Rendered markdown: {state_dir / 'ui_design.md'}")


@app.command("list-runs")
def list_runs(
    project_dir: Path = typer.Option(
        ...,
        "--project-dir",
        "-d",
        help="Path to the managed project directory.",
        show_default=False,
    ),
) -> None:
    """
    List all runs for a project, ordered newest first.

    Outputs a JSON array to stdout. Each element contains:
      run_id, status, started_at, run_mode, brief, escalation_reason, failed_phase, agent_call_count

    escalation_reason and failed_phase are non-null only when the latest escalation
    is unresolved (i.e. the run is awaiting a resume decision).
    """
    run_log_dir = _run_log_dir(project_dir)
    if not run_log_dir.exists():
        typer.echo("[]")
        return

    summaries: list[dict[str, Any]] = []
    for entry in run_log_dir.iterdir():
        if not entry.is_dir():
            continue
        summary = _read_run_summary(run_log_dir, entry.name)
        if summary is not None:
            summaries.append(summary)

    summaries.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    typer.echo(json.dumps(summaries, indent=2))


# ---------------------------------------------------------------------------
# Config loading helper
# ---------------------------------------------------------------------------

def _load_config_or_exit(project_dir: Path) -> Any:
    """Load config with full validation; exit with a friendly message on failure."""
    try:
        return load_config(
            project_dir,
            require_sandbox_image=True,
            require_repos=False,
            require_env_vars=True,
        )
    except (ValueError, EnvironmentError, FileNotFoundError) as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(1)
