---
description: Review all changed CodeForge source code on the current branch. Checks for bugs, edge cases, design drift, test coverage, type correctness, and structural rule violations.
allowed-tools: Read, Bash, Glob, Grep
model: claude-sonnet-4-6
argument-hint: [feature-name (optional — used to find plan doc if auto-discovery is ambiguous)]
---

# CodeForge Source Code Review

You are performing a thorough code review of changes made to the CodeForge pipeline source on the current branch. This is equivalent to a senior engineer reviewing a PR before merge. You are not the author of this code — review it critically and independently.

## Step 1 — Establish the diff

Capture the current branch name:

```
!`git branch --show-current`
```

Run the following to identify all changed files relative to the branch point:

```
!`git diff $(git merge-base HEAD main) --name-only`
```

Also capture the full diff for reading:

```
!`git diff $(git merge-base HEAD main)`
```

And the commit log for context on what was done incrementally:

```
!`git log --oneline $(git merge-base HEAD main)..HEAD`
```

If the changed file list is empty, stop and report: "No changes detected relative to main. Confirm you are on a feature branch."

## Step 2 — Find the plan document

Look for the most recently modified markdown file in `.claude/plans/`:

```
!`ls -lt .claude/plans/ 2>/dev/null | head -20`
```

If `$ARGUMENTS` was provided, use it as a hint to match a filename. Otherwise take the most recently modified `.md` file in that directory.

If `.claude/plans/` is empty or does not exist, check for any `.md` file modified in the last 4 hours outside of `codeforge/` and `tests/`:

```
!`find . -name "*.md" -newer ./codeforge/__main__.py -not -path "./codeforge/*" -not -path "./tests/*" -not -path "./.git/*" 2>/dev/null`
```

Read whichever plan file you find. If no plan file is found, note this explicitly and proceed — you will still review for correctness but cannot check design intent.

## Step 3 — Read all changed files in full

Read every file listed in the diff output from Step 1. Do not skim — read each file completely. You need full context to reason about correctness.

For each changed file, also identify its immediate dependencies within the CodeForge source tree:
- What does this file import from within `codeforge/`?
- What imports this file from within `codeforge/`?

Read those dependency files too, even if they were not changed. You need to verify that the changed code is consistent with what it calls and what calls it.

Use Grep to find cross-references if needed:
```
!`grep -r "from codeforge.<module>" codeforge/ --include="*.py" -l`
```

## Step 4 — Run mypy on changed files

From the file list produced in Step 1, filter to only `.py` files. Construct and run a Bash command of the form:

```
python -m mypy file1.py file2.py file3.py --ignore-missing-imports 2>&1
```

Use the Bash tool directly — do not use a shell backtick block for this step. Build the command string from the actual filenames you collected in Step 1.

If there are no `.py` files in the changed set, skip this step and note "no Python files changed."

Collect all errors and warnings. These feed directly into the review findings.

## Step 5 — Perform the review

Now conduct the full review across these dimensions. Be thorough and specific — cite file names and line numbers for every finding.

### 5A — Correctness and bugs

- Logic errors: does the code do what it appears to intend?
- Off-by-one errors, incorrect conditionals, wrong operator precedence
- Incorrect use of Pydantic v2 patterns (prefer `**data` constructor calls over `model_validate()` in typed contexts; use `cast()` for Literal narrowing; explicit `is not None` guards before accessing optional fields like `budget_tokens`)
- Any mypy errors found in Step 4

### 5B — Design conformance

Compare the implementation against the plan document from Step 2. For every requirement or design decision stated in the plan:
- Is it implemented?
- Is it implemented correctly?
- Did the implementation diverge from the plan in any way? If so, is the divergence justified or is it drift?

Flag anything the plan specified that is missing from the implementation.

### 5C — CodeForge structural rules

These are hard rules. Any violation is a blocker:

- `orchestrator/` is the only package that imports from `agents/`, `firewall/`, `store/`, and `model_router/`. Agents must never import from each other.
- `schemas/contracts.py` imports nothing from within the CodeForge package. It is a leaf node — zero internal imports.
- `model_router/router.py` is the only file that imports LiteLLM. No other file may import LiteLLM.
- `store/project_state.py` reads JSON and generates markdown renders. It must never parse markdown.
- Agents communicate only through the artifact store — never directly with each other.
- The firewall assembler (`firewall/assembler.py`) must be a pure deterministic function — no LLM calls, no stochastic behaviour.
- All project state document writes must go through the `pending_writes` map, never directly to disk during a run.
- The LLM never makes allow/deny decisions in the firewall — that is always a code function reading a manifest.

### 5D — Edge cases and error handling

- What happens when optional fields are `None`? Are all `None` paths handled?
- What happens when an agent returns an unexpected output shape?
- What happens when a file or directory does not exist?
- What happens on the first run of a new project (empty state, no prior artifacts)?
- What happens in continuation mode when a prior artifact has a mismatched `schema_version`?
- Are all counter increments bounded by their configured limits? Is the check `<` not `<=`?
- Are retry loops correctly terminated — no possibility of infinite loops?
- Are all `EscalationReason` and `RoutingDecision` enum variants handled, or are there missing cases?

### 5E — Information barrier / firewall correctness

- Does the changed code respect the firewall manifest (Part 5 of agent-contracts)?
- Does any new agent receive an artifact type it is listed as a `forbidden_consumer` of?
- If a whitelist projection was added or modified (constructing `CoderRetryContext`, `CodeBugFinding`, test_bug retry context), does it copy *only* the whitelisted fields? Are `stack_trace`, `assertion` text, `error_message`, `evidence`, and test code provably not copied?
- Is `stripped_fields` logged on the handoff event when anything is stripped?

### 5F — Event log correctness

- Are `gate` events emitted for every validation rule check?
- Does every agent invocation produce a `handoff` event with the correct `invocation_type`?
- Does every routing decision produce a `routing` event with the correct `routing_table_row`?
- Does the `counters` snapshot on each event reflect the state *at the moment of emission*, not before or after?

### 5G — Test coverage

Look at the changed source files. For each logical unit of new or changed behaviour:

- Does a corresponding test exist in `codeforge/tests/`?
- Does the test cover the golden path (expected input, expected output)?
- Does the test cover the key edge cases identified in 5D above?
- Are error paths tested — not just happy paths?
- If a whitelist projection function was added or modified, is it unit-tested as a pure function?

List any changed source logic that has no test coverage. This is a warning by default, a blocker if the untested code is a gate, a firewall rule, or a whitelist projection.

---

## Step 6 — Produce the report

Output the findings in this exact structure. Omit any section that has zero findings — do not pad with "No issues found" under every heading.

---

### CODEFORGE REVIEW REPORT
**Branch:** [current branch name from Step 1]  
**Diff base:** `main` (merge-base)  
**Files changed:** [count]  
**Plan doc:** [filename or "not found"]  
**mypy:** [PASS / N errors]

---

#### 🔴 BLOCKERS — must fix before merge

[Number each finding. Format: `B-1`, `B-2`, etc.]

**B-1 · [Category] · `filename.py:line`**  
[What the problem is. What the correct behaviour should be. Why this is a blocker.]

---

#### 🟡 WARNINGS — worth fixing, not blocking

[Number each finding. Format: `W-1`, `W-2`, etc.]

**W-1 · [Category] · `filename.py:line`**  
[What the problem is. Suggested fix or direction.]

---

#### 🔵 NOTES — observations, no action required

[Number each finding. Format: `N-1`, `N-2`, etc.]

**N-1 · [Category] · `filename.py:line`**  
[Observation.]

---

#### TEST GAPS

List any changed logic with missing or thin test coverage:

- `filename.py` — [what behaviour lacks a test] — [BLOCKER / WARNING]

---

#### DESIGN DRIFT

List any divergences between the plan doc and the implementation:

- [Plan said X. Implementation does Y. Assessment: justified / unjustified drift.]

---

#### CONTEXT LOAD ESTIMATE
**Files read:** [count of source files read]  
**Approximate review size:** [rough token estimate — count changed files × ~200 + findings × ~100]  
**Recommendation:** [If estimate < 6,000 tokens: "Take this report back to the original implementation session." If ≥ 6,000 tokens: "Consider a new session — paste this report + the plan doc as inputs."]

---

#### SUMMARY
**Blockers:** [count]  
**Warnings:** [count]  
**Notes:** [count]  
**Test gaps:** [count]  
**Verdict:** [MERGE WHEN FIXED / NEEDS WORK / DO NOT MERGE]

---

Be direct. If something is wrong, say it clearly. Do not soften findings to be polite — the goal is to catch problems before they reach the pipeline runtime.