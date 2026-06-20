Investigate a codeforge run and produce a structured diagnosis with a source-level fix recommendation.

Arguments: $ARGUMENTS — a run id, plus an OPTIONAL project name or run-dir path.
Examples: `run-7a84ce2311b0`, `run-7a84ce2311b0 release-notes`,
`run-7a84ce2311b0 /home/sabbamonte/projects/release-notes/codeforge-state/run-logs/run-7a84ce2311b0`.

This is a READ-ONLY investigation. Never resume, edit, or run the pipeline. Your job
is to read the right files in the right order, walk the reference chain, and identify
what needs to change in the Codeforge source — a prompt, gate, schema, or agent
contract — so this class of failure does not recur. Do not suggest one-off reprompts
or resume instructions; the goal is a fix to the framework.

## 0. Locate the run

Tolerate a missing `run-` prefix (try both `<id>` and `run-<id>`).

- Hint is a project name → `/home/sabbamonte/projects/<hint>/codeforge-state/run-logs/<run_id>/`.
- Hint is a path → use it directly as the run dir.
- No hint → glob `/home/sabbamonte/projects/*/codeforge-state/run-logs/<run_id>/`.
  - Exactly one match → use it.
  - Several matches → list them and ask which one (or to re-run with a hint). Stop.
  - Zero matches → report the glob you tried and stop.

The run dir contains: `brief.txt`, `codeforge_run.json`, `events.jsonl`, and the
directories `artifacts/`, `failed_artifacts/`, `raw_outputs/`, `context_packages/`.

## 1. Orient (cheap reads first)

Read `brief.txt` and `codeforge_run.json`. From the run JSON report:
- `status` — one of `running`, `awaiting_human`, `succeeded`, `failed_escalated`, `failed_terminal`.
- `agent_call_count` and the non-zero `retry_counters` (which loop spent its budget).
- One line per entry in `escalations[]`: its `reason`, whether `resolved`, and
  `suggested_reentry_state`.
- If there is MORE THAN ONE escalation, say so — this is a re-failure after a resume,
  not a fresh failure, and the latest escalation is the relevant one.

`escalations[]` is the diagnosis spine. Each entry has: `reason`, `agent_output_ref`
(the artifact_id that triggered it), `resolved`, `resolution` (`outcome`,
`reentry_directive`, `human_notes`), `suggested_reentry_state`.

`EscalationReason` is one of: `max_retries_exceeded`, `global_ceiling_exceeded`,
`malformed_output`, `output_truncated`, `block_flag`, `low_confidence`,
`human_required`, `commit_failure`, `schema_version_mismatch`.

## 2. Find the decisive event in events.jsonl

`events.jsonl` is one JSON object per line. Order by `sequence` (authoritative — NEVER
trust `timestamp` for ordering). It can be 30KB+, so read the tail first and only scan
higher up when the tail does not explain the failure (e.g. a budget exhausted by
repeated failures earlier in a loop).

Find:
- The last `routing` event whose `decision` is `escalate`, `terminal`, or `await_human`.
  Quote its `routing_table_row`, `decision`, `next_state`, and `detail`.
- The `gate` events with `passed=false` leading up to it. Quote their `rule` and `detail`.
- The `counters` snapshot on the final event — confirms which budget was exhausted.

The `detail` strings on gate/routing events are written to be self-sufficient for
diagnosis (e.g. `error_phase=no_results_json (budget exhausted) | pytest did not
produce results.json`). Quote them; don't paraphrase away the specifics.

Event types and their key fields: `handoff` (`to_agent`, `invocation_type`,
`assembly_id`, `reprompt_reason`), `gate` (`rule`, `passed`, `artifact_ref`,
`detail`), `routing` (`routing_table_row`, `decision`, `counter_deltas`,
`counter_resets`, `next_state`, `detail`), `state_write`, `human_interaction`.

## 3. Walk the chain to the root artifact

Find the root artifact id, in this order:
1. the relevant escalation's `agent_output_ref`;
2. if that is empty (common for `human_required` escalations raised by routing
   exhaustion rather than a bad artifact), the failing `gate` event's `artifact_ref`;
3. if still none, the latest relevant artifact from the run JSON's `artifacts` map
   (e.g. `test_results` / `test_analysis` when the decisive `detail` is about tests).

Open `artifacts/<id>.json` first, then `failed_artifacts/<id>.json`.

Artifact shape: `{ meta{ artifact_id, artifact_type, produced_by, ... },
output{ output, assumptions_made, confidence, unresolved_flags[] } }`.
Report `produced_by`, `confidence`, and any `unresolved_flags` — call out
`severity: block` explicitly. For test artifacts, report `verdict` and
`failure_analyses`.

## 4. Deepen based on the real signal (only what the case calls for)

The escalation `reason` label can be coarse: a routing-exhaustion failure is recorded
as `human_required` even when the true cause is a test error. So if the decisive
routing `detail` contains `error_phase=...`, follow the test-execution branch below
regardless of the escalation `reason`. Let the `detail` and `error_phase` drive you,
not just the reason label.

- `low_confidence` / `block_flag` → find the producing `handoff` event's
  `assembly_id`, open `context_packages/<assembly_id>.json`, and check `access_events`
  for `deny` decisions (with `reason_code`). A missing input the agent was denied is a
  common root cause.
- `malformed_output` / `output_truncated` → open `raw_outputs/<id>.json`
  (`{artifact_id, produced_by, raw}`) to see the exact string the model produced —
  truncation, invalid JSON, or wrong field types.
- `max_retries_exceeded` / `global_ceiling_exceeded` → identify the looping agent from
  the non-zero `retry_counters`, then summarize the repeated failing-gate `detail`s to
  show WHY each attempt failed (usually the same unmet gate).
- test execution errors → `error_phase` is the key signal and it is reliably present
  in the decisive routing `detail` (from Step 2); a persisted `test_results` /
  `test_runner_results` artifact (with `stdout_tail` / `stderr_tail`) is a bonus when
  present but is NOT always written to disk. The latest `test_analysis` artifact IS
  usually present — surface its `verdict` and `failure_analyses[].root_cause_hypothesis`
  + `evidence`. `error_phase` deterministically names the owning agent:
  - `missing_requirements_txt`, `runtime_dep_install_failed` → coder
  - `test_dep_install_failed`, `no_results_report`, `pytest_exit_error` → test_designer
- `human_required` → summarize the pending `human_interaction` and `suggested_reentry_state`.
- `commit_failure` → focus on the commit-phase routing/gate `detail`.

## 4a. Locate the fix target in Codeforge source

Every failure — whether a prompt gap, a missing contract, a schema mismatch, or an
access-policy hole — maps to a specific file in the Codeforge source tree. Identify it:

- **Prompt gap** — the agent lacked a rule or example it needed. Fix: the rendered
  prompt file under `prompts/` (find the template that produces it). The gap is usually
  one of: a missing enum list, a missing constraint, or a missing worked example.
- **Gate misconfiguration** — a gate fires on output the agent could not have known was
  invalid (undocumented rule, newly tightened constraint with no prompt update, or a rule
  that fires identically on every retry). Fix: either the gate implementation or the
  prompt that tells the agent how to satisfy it — often both need updating together.
- **Access/contract gap** — an agent was denied a file it needed, or two agents made
  incompatible choices because one cannot see the other's output (e.g., test_designer
  forbidden from reading code_artifact). Fix: the context-package assembly logic or the
  `allowed_consumers` / `forbidden_consumers` policy for the relevant artifact type, OR
  add a derived summary artifact that bridges the gap without exposing the full artifact.
- **Schema mismatch** — the agent's output schema diverges from what downstream
  consumers or gates expect. Fix: the schema definition and the corresponding prompt
  section that documents valid values.

In each case, name the Codeforge source path(s) precisely. If you cannot determine the
path from the run artifacts alone, name the component (e.g., "security_reviewer prompt
template" or "test_suite artifact schema") and describe what to grep for.

## 5. Report (inline only — write nothing to disk)

Emit a structured report:

1. **Run** — one-line brief, `status`, and the run dir path.
2. **Where it failed** — the decisive event: `sequence`, row/rule, and quoted `detail`.
3. **Root cause** — the artifact / flag / raw-output / `error_phase` evidence. Give a
   clickable file path for every artifact you cite so the human can dig further.
4. **Fix target** — name the Codeforge source component(s) to change (prompt template,
   gate, schema, access policy). One component per bullet. For each, describe precisely
   what is wrong and what the correct behaviour should be.
5. **If inconclusive** — name the specific files and `sequence` numbers to read next.

Do not suggest resume options, reprompt workarounds, or run-specific patches. The
output of this investigation is a to-do list for the framework, not instructions for
salvaging the failed run.

Prefer the deterministic fields (`status`, `error_phase`, `retry_counters`, escalation
`reason`, gate/routing `detail`) before interpreting any free-text. Be selective — read
the chain, don't dump whole files.
