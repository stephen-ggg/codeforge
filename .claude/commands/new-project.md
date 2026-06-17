Create a new codeforge project. `$ARGUMENTS` is `<name> [stack]` where `stack` is optional
and defaults to `python`. Supported stacks: `python`, `nextjs-supabase`.

Parse `$ARGUMENTS`: the first token is `<name>`, the second (if present) is `<stack>`. If the
stack is omitted, use `python`. If the stack is not one of the supported values, stop and ask.

Read the chosen profile at
`/home/sabbamonte/codeforge/codeforge/stacks/profiles/<stack>.yaml` — you will copy its
`default_sandbox_image` into the config and its `seed_tech_decisions` into the seeded
`tech_stack.json` below.

---

## Common steps (both stacks)

1. Create the directory structure:
   - /home/sabbamonte/projects/<name>/
   - /home/sabbamonte/projects/<name>/codeforge-state/
   - /home/sabbamonte/projects/<name>/codeforge-state/.codeforge/
   - /home/sabbamonte/projects/<name>/codeforge-state/project-state/
   - /home/sabbamonte/projects/<name>/codeforge-state/run-logs/
   - /home/sabbamonte/projects/<name>/source/

2. Create /home/sabbamonte/projects/<name>/codeforge-state/.codeforge/codeforge.config.yaml.
   Set `stack.profile` and `test_runner.sandbox_image` to the chosen stack (sandbox_image =
   the profile's `default_sandbox_image`):

   ```yaml
   # Project-local codeforge configuration for "<name>".
   # Overrides the codeforge installation defaults; only set fields that differ.

   stack:
     profile: "<stack>"            # "python" or "nextjs-supabase"

   repos:
     codeforge_state:
       remote: ""          # TODO: git remote URL for this codeforge state repo (required before push)
       branch: "main"

     source_code:
       path: "/home/sabbamonte/projects/<name>/source"
       remote: ""          # TODO: git remote URL, used to open PRs (required before push)
       default_branch: "main"
       branch_prefix: "codeforge/"
       pr_target: "main"
       auto_merge: false
       output_dir: "src"   # informational only — the coder's file paths are written verbatim
                           # at the repo root, so this does not affect placement.

   test_runner:
     sandbox_image: "<default_sandbox_image>"   # python:3.12-slim | node:20-bookworm
     timeout_seconds: 300
     environment_vars: {}
   ```

3. Seed /home/sabbamonte/projects/<name>/codeforge-state/project-state/tech_stack.json with the
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

4. Create /home/sabbamonte/projects/<name>/.envrc (direnv loads it for the project root and all
   subdirs). For **both** stacks include the Anthropic key; for **nextjs-supabase** also add the
   Supabase placeholders:

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

5. Create /home/sabbamonte/projects/<name>/codeforge-state/.gitignore containing:
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

### nextjs-supabase
- Create source dirs: source/app/, source/components/, source/lib/, source/public/.
- Create a minimal, working Next.js + TypeScript base in source/:
  - `package.json` — matching the baseline in the profile's coder prompt fragment
    (`next`, `react`, `react-dom`, `@supabase/supabase-js`; devDeps `typescript`,
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
- git -C .../source/ add .gitignore  (plus the scaffolded files for nextjs-supabase)
- git -C .../source/ commit -m "chore: initialize repository"

---

## Confirmation summary

Print:
```
✓ Stack:        <stack>
✓ State repo:   /home/sabbamonte/projects/<name>/codeforge-state/
✓ Source repo:  /home/sabbamonte/projects/<name>/source/
✓ Both repos initialised on `main` with a base commit (required by codeforge)
✓ tech_stack.json seeded with the stack's locked decisions
✓ Config pre-filled: stack.profile + sandbox_image (<default_sandbox_image>)
✓ .envrc created at project root (.gitignore'd in both repos)
```
For nextjs-supabase also note: `✓ Next.js + TypeScript base scaffolded; npm install run`.

Then list manual steps still required:
  • Add your key to /home/sabbamonte/projects/<name>/.envrc:
      export ANTHROPIC_API_KEY="sk-ant-..."
    then run `direnv allow` in the project root. (required — the run fails to start without it)
  • For nextjs-supabase: create a Supabase project and paste its URL + anon key + service-role
    key into the same .envrc (required for runtime; tests mock Supabase so they pass without it).
  • Pull the sandbox image so test runs work: `docker pull <default_sandbox_image>`.
  • For pushing / opening PRs (not needed for local test runs):
      - add  export PIPELINE_GITHUB_TOKEN="..."  to the same .envrc
      - set repos.codeforge_state.remote and repos.source_code.remote in the config.
