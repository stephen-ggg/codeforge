---
description: Create a branch off main, commit current changes, generate a PR title and description, and open the PR. Pass an optional branch name as the argument.
allowed-tools: Bash, Read
argument-hint: [branch-name (optional — auto-derived from the diff if omitted)]
---

# Ship — Branch, Commit, and Open PR

You are creating a branch, committing the current working-tree changes, and opening a
GitHub pull request. Work through these steps in order. Do not skip steps.

## Step 1 — Gather context

Capture the current branch, diff, and recent commits:

```
!`git branch --show-current`
```

```
!`git status --short`
```

```
!`git diff`
```

```
!`git log main -5 --oneline`
```

```
!`git diff main --stat`
```

## Step 2 — Determine the branch name

If `$ARGUMENTS` was provided, use it verbatim as the branch name.

Otherwise, derive a short kebab-case name from the nature of the changes (e.g.
`fix/coder-confidence-retry`, `feat/module-interfaces-for-test-designer`). The name must:
- Start with `fix/` for bug fixes or `feat/` for new capabilities
- Be under 60 characters
- Contain only lowercase letters, digits, and hyphens

## Step 3 — Create the branch or stay on the current one

If the current branch is `main`:
```
!`git checkout -b <branch-name>`
```

If the current branch is already a feature branch (not `main`), stay on it — do not
create a new branch.

## Step 4 — Stage and commit

Stage all modified tracked files. Do NOT stage untracked files unless they are clearly
part of the change (e.g. a new source file created as part of the work):

```
!`git add <file1> <file2> ...`
```

If there is nothing to commit (all changes already committed), skip the commit step and
note this.

Write a commit message that:
- Starts with a type prefix (`fix:`, `feat:`, `refactor:`, `test:`, `docs:`)
- Summarises the WHY, not the what
- Is 72 characters or fewer on the subject line
- Includes the Co-Authored-By trailer

Use a HEREDOC:
```
git commit -m "$(cat <<'EOF'
<subject line>

<optional body — 1-3 sentences on motivation if needed>

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

## Step 5 — Generate the PR title

Write a PR title that:
- Is under 70 characters
- Uses the imperative mood ("add", "fix", "replace", not "added" or "fixes")
- Describes the change at the feature level, not the file level

## Step 6 — Generate the PR description

Write the description in this exact format — no other format is acceptable:

```
## Summary

- **<bold feature title>** — <one or two sentence explanation of what changed and why.
  Be specific: name the artifact, counter, prompt section, or route that changed.>

- **<bold feature title>** — <explanation>

(one bullet per logical change; group tightly related changes into one bullet)

## Test plan

- [ ] `<pytest command or manual check>` — <what it covers>
- [ ] `<pytest command or manual check>` — <what it covers>
- [ ] `pytest tests/` — full suite green
```

Rules for the description:
- Each bullet uses `**bold title** — prose`. The bold title is 2–5 words naming the
  change. The prose explains the motivation and effect.
- The test plan must include a `pytest tests/` full-suite check if any Python files
  changed.
- Do not add any other sections (no "Problem", no "Background", no headers beyond
  `## Summary` and `## Test plan`).

## Step 7 — Push and open the PR

Push the branch:
```
!`git push -u origin <branch-name>`
```

Open the PR using `gh pr create` with a HEREDOC body so markdown is preserved exactly:

```
gh pr create --title "<title from Step 5>" --body "$(cat <<'EOF'
## Summary

- **...** — ...

## Test plan

- [ ] ...
EOF
)"
```

After the PR is created, output the PR URL so the user can navigate to it.
