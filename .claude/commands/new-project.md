Create a new codeforge project. `$ARGUMENTS` is `<name> [stack]` where `stack` is optional
and defaults to `python`. Supported stacks: `python`, `nextjs`, `nextjs-supabase`.

Parse `$ARGUMENTS`: the first token is `<name>`, the second (if present) is `<stack>`. If the
stack is omitted, use `python`. If the stack is not one of the supported values, stop and ask.

## Paths

This skill uses two base paths. Resolve them once at the start and substitute the real
absolute paths into every command and file path below — do **not** hardcode any home
directory.

- **`$CODEFORGE_HOME`** — the root of this CodeForge install (the repo this skill lives in).
  Resolve it from the repo root, e.g. run `git rev-parse --show-toplevel` from the directory
  this skill file lives in (the parent of `.claude/commands/`).
- **`$PROJECTS_ROOT`** — where managed projects live. Default to `$HOME/projects`; create it
  if it does not exist. (If the user has named a different projects location, use that.)

Read the chosen profile at
`$CODEFORGE_HOME/codeforge/stacks/profiles/<stack>.yaml` — you will copy its
`default_sandbox_image` into the config and its `seed_tech_decisions` into the seeded
`tech_stack.json` below.

---

## Common steps (both stacks)

1. Create the directory structure:
   - $PROJECTS_ROOT/<name>/
   - $PROJECTS_ROOT/<name>/codeforge-state/
   - $PROJECTS_ROOT/<name>/codeforge-state/.codeforge/
   - $PROJECTS_ROOT/<name>/codeforge-state/project-state/
   - $PROJECTS_ROOT/<name>/codeforge-state/run-logs/
   - $PROJECTS_ROOT/<name>/source/

2. Create $PROJECTS_ROOT/<name>/codeforge-state/.codeforge/codeforge.config.yaml as a
   **full copy of the installation default settings**, then customize it for this project. Start
   by copying the entire file
   `$CODEFORGE_HOME/codeforge/config/codeforge.config.yaml` verbatim — this gives the
   user every tunable (retry limits, ceilings, confidence thresholds, per-agent model settings,
   …) already present and ready to edit. The project config is deep-merged over the installation
   default, so an exact copy is a safe, behaviour-neutral baseline; the user can then change any
   field without having to discover it first.

   After copying, apply these project-specific edits to the copy:

   - Replace the top-of-file `name:`/`schema_version:` lines with a project header comment, e.g.:
     ```yaml
     # Project-local codeforge configuration for "<name>".
     # Full copy of the installation defaults — edit any field to customize this project.
     # This file is deep-merged over codeforge/config/codeforge.config.yaml at load time.
     ```
     (Keep the `name:` and `schema_version:` keys themselves.)
   - Set `stack.profile` to the chosen stack:
     ```yaml
     stack:
       profile: "<stack>"            # "python", "nextjs", or "nextjs-supabase"
     ```
   - Set `test_runner.sandbox_image` to the profile's `default_sandbox_image` (leave the other
     `test_runner` fields from the default in place):
     ```yaml
     test_runner:
       sandbox_image: "<default_sandbox_image>"   # python:3.12-slim | node:20-bookworm
     ```
   - Add a `repos:` block (the installation default has none — it is required per-project):
     ```yaml
     repos:
       codeforge_state:
         remote: ""          # TODO: git remote URL for this codeforge state repo (required before push)
         branch: "main"

       source_code:
         path: "$PROJECTS_ROOT/<name>/source"
         remote: ""          # TODO: git remote URL, used to open PRs (required before push)
         default_branch: "main"
         branch_prefix: "codeforge/"
         pr_target: "main"
         auto_merge: false
         output_dir: "src"   # informational only — the coder's file paths are written verbatim
                             # at the repo root, so this does not affect placement.
     ```

3. Seed $PROJECTS_ROOT/<name>/codeforge-state/project-state/tech_stack.json with the
   profile's locked decisions so the architecture designer designs within the stack and the
   first run does not re-derive them. Take the entries from the profile's `seed_tech_decisions`
   and write:

   ```json
   {
     "schema_version": "1.0.0",
     "decisions": [
       { ...each seed_tech_decision..., "run_id": "project-init", "confirmed_at": "<ISO-8601 now>" }
     ]
   }
   ```

   (Add `run_id` and `confirmed_at` to each seeded decision; keep `id`, `domain`, `decision`,
   `rationale`, `locked`, `record` verbatim from the profile.)

4. Create $PROJECTS_ROOT/<name>/.envrc (direnv loads it for the project root and all
   subdirs). All stacks include the Anthropic key; **nextjs-supabase** also adds Supabase
   placeholders:

   ```bash
   # Per-project environment, auto-loaded by direnv. Paste your real values below.
   export ANTHROPIC_API_KEY=""
   # nextjs-supabase only:
   export NEXT_PUBLIC_SUPABASE_URL=""
   export NEXT_PUBLIC_SUPABASE_ANON_KEY=""
   export SUPABASE_SERVICE_ROLE_KEY=""
   ```

   For **nextjs-supabase**, also wire these three Supabase vars into
   `test_runner.environment_vars` in the config from step 2 so the sandbox sees them:

   ```yaml
   test_runner:
     environment_vars:
       NEXT_PUBLIC_SUPABASE_URL: "${NEXT_PUBLIC_SUPABASE_URL}"
       NEXT_PUBLIC_SUPABASE_ANON_KEY: "${NEXT_PUBLIC_SUPABASE_ANON_KEY}"
       SUPABASE_SERVICE_ROLE_KEY: "${SUPABASE_SERVICE_ROLE_KEY}"
   ```

   For **nextjs** (no Supabase), the `.envrc` only needs `ANTHROPIC_API_KEY`.

5. Create $PROJECTS_ROOT/<name>/codeforge-state/.gitignore containing:
   ```
   run-logs/
   .envrc
   ```

6. Initialise the codeforge-state repo on a `main` branch with a base commit so `main` always
   exists (codeforge requires it):
   - git -C .../codeforge-state/ init -b main
   - git -C .../codeforge-state/ add .gitignore project-state/tech_stack.json
   - git -C .../codeforge-state/ commit -m "chore: initialize repository"
     (set a local user.name/user.email on the repo first if git complains)

---

## Stack-specific source scaffolding

### python
- Create source dirs: source/src/ and source/tests/.
- Create a .venv: `python3.12 -m venv .venv` in source/.
- source/.gitignore:
  ```
  __pycache__/
  *.pyc
  .venv/
  .envrc
  ```

### nextjs
- Create source dirs: source/app/, source/components/, source/lib/, source/public/.
- Create a minimal, working Next.js + TypeScript base in source/:
  - `package.json` — (`next`, `react`, `react-dom`; devDeps `typescript`,
    `@types/react`, `@types/node`, `vitest`, `@testing-library/react`,
    `@testing-library/jest-dom`, `jsdom`). Include `"scripts": {"dev":"next dev",
    "build":"next build","test":"vitest run"}`.
  - `tsconfig.json` — `"strict": true`, with the `@/*` path alias mapped to the repo root.
  - `next.config.mjs` — minimal `export default {}`.
  - `vitest.config.ts` — `environment: "jsdom"`, with the `@` alias resolving to the repo root.
  - `app/page.tsx` — a trivial placeholder page so the base builds.
  - `app/layout.tsx` — a minimal root layout.
- source/.gitignore:
  ```
  node_modules/
  .next/
  .env*
  .envrc
  ```
- Run `npm install` in source/ to produce node_modules + package-lock.json (local dev; the
  sandbox installs from package.json independently).

Then initialise the source repo on a `main` branch with a base commit (the immutable base
every run branches from):
- git -C .../source/ init -b main
- git -C .../source/ add .gitignore (plus the scaffolded files)
- git -C .../source/ commit -m "chore: initialize repository"

### nextjs-supabase
- Same as `nextjs` above, but also include `@supabase/supabase-js` in `dependencies`.
- source/.gitignore: same as nextjs.
- Run `npm install` in source/.

Then initialise the source repo on a `main` branch with a base commit (the immutable base
every run branches from):
- git -C .../source/ init -b main
- git -C .../source/ add .gitignore (plus the scaffolded files)
- git -C .../source/ commit -m "chore: initialize repository"

---

## Confirmation summary

Print:
```
✓ Stack:        <stack>
✓ State repo:   $PROJECTS_ROOT/<name>/codeforge-state/
✓ Source repo:  $PROJECTS_ROOT/<name>/source/
✓ Both repos initialised on `main` with a base commit (required by codeforge)
✓ tech_stack.json seeded with the stack's locked decisions
✓ Config seeded from installation defaults (full copy, ready to customize); stack.profile + sandbox_image (<default_sandbox_image>) + repos block set
✓ .envrc created at project root (.gitignore'd in both repos)
```
For nextjs and nextjs-supabase also note: `✓ Next.js + TypeScript base scaffolded; npm install run`.

Then list manual steps still required:
  • Add your key to $PROJECTS_ROOT/<name>/.envrc:
      export ANTHROPIC_API_KEY="sk-ant-..."
    then run `direnv allow` in the project root. (required — the run fails to start without it)
  • For nextjs-supabase only: create a Supabase project and paste its URL + anon key + service-role
    key into the same .envrc (required for runtime; tests mock Supabase so they pass without it).
  • Pull the sandbox image so test runs work: `docker pull <default_sandbox_image>`.
  • For pushing / opening PRs (not needed for local test runs):
      - add  export PIPELINE_GITHUB_TOKEN="..."  to the same .envrc
      - set repos.codeforge_state.remote and repos.source_code.remote in the config.
