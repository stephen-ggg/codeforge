Create a new codeforge project named $ARGUMENTS.

1. Create the directory structure:
   - /home/sabbamonte/projects/$ARGUMENTS/
   - /home/sabbamonte/projects/$ARGUMENTS/codeforge-state/
   - /home/sabbamonte/projects/$ARGUMENTS/codeforge-state/.codeforge/
   - /home/sabbamonte/projects/$ARGUMENTS/codeforge-state/project-state/
   - /home/sabbamonte/projects/$ARGUMENTS/codeforge-state/run-logs/
   - /home/sabbamonte/projects/$ARGUMENTS/source/
   - /home/sabbamonte/projects/$ARGUMENTS/source/src/
   - /home/sabbamonte/projects/$ARGUMENTS/source/tests/

2. Create /home/sabbamonte/projects/$ARGUMENTS/codeforge-state/.codeforge/codeforge.config.yaml containing:
   # Project-local codeforge configuration for "$ARGUMENTS".
   # Overrides the codeforge installation defaults; only set fields that differ.

   repos:
     codeforge_state:
       remote: ""          # TODO: git remote URL for this codeforge state repo (required before push)
       branch: "main"

     source_code:
       path: "/home/sabbamonte/projects/$ARGUMENTS/source"
       remote: ""          # TODO: git remote URL, used to open PRs (required before push)
       default_branch: "main"
       branch_prefix: "codeforge/"
       pr_target: "main"
       auto_merge: false      # local main is canonical and accumulates by fast-forward;
                              # the PR is for review. Enable only if you want the remote
                              # to auto-squash-merge (diverges remote history from local).
       output_dir: "src"

   test_runner:
     sandbox_image: "python:3.12-slim"
     timeout_seconds: 300
     environment_vars: {}

3. Create /home/sabbamonte/projects/$ARGUMENTS/.envrc containing (direnv loads this
   for the project root and all subdirs, so the key is set wherever you run codeforge):
   # Per-project environment, auto-loaded by direnv. Paste your real key below.
   export ANTHROPIC_API_KEY=""

4. Create /home/sabbamonte/projects/$ARGUMENTS/codeforge-state/.gitignore containing:
   run-logs/
   .envrc

5. Initialise the codeforge-state repo on a `main` branch with a base commit so `main`
   always exists (codeforge requires it; an unborn HEAD is treated as a setup error):
   - git -C /home/sabbamonte/projects/$ARGUMENTS/codeforge-state/ init -b main
   - git -C /home/sabbamonte/projects/$ARGUMENTS/codeforge-state/ add .gitignore
   - git -C /home/sabbamonte/projects/$ARGUMENTS/codeforge-state/ commit -m "chore: initialize repository"
     (commits the .gitignore from step 4; if git complains about identity, set a local
     user.name/user.email on the repo first)

6. Create a .venv in /home/sabbamonte/projects/$ARGUMENTS/source/ using python3.12 -m venv .venv

7. Create /home/sabbamonte/projects/$ARGUMENTS/source/.gitignore containing:
   __pycache__/
   *.pyc
   .venv/
   .envrc

8. Initialise the source repo on a `main` branch with a base commit, the same way. This
   is the immutable base every run's feature branch is created from; the canonical
   checkout must rest on `main` between runs:
   - git -C /home/sabbamonte/projects/$ARGUMENTS/source/ init -b main
   - git -C /home/sabbamonte/projects/$ARGUMENTS/source/ add .gitignore
   - git -C /home/sabbamonte/projects/$ARGUMENTS/source/ commit -m "chore: initialize repository"
     (commits the .gitignore from step 7; set a local user.name/user.email first if needed)

Print a confirmation summary:
✓ State repo:   /home/sabbamonte/projects/$ARGUMENTS/codeforge-state/
✓ Source repo:  /home/sabbamonte/projects/$ARGUMENTS/source/
✓ Both repos initialised on `main` with a base commit (required by codeforge)
✓ .venv created in source repo (python3.12)
✓ Config pre-filled: source path + sandbox_image (python:3.12-slim)
✓ .envrc created at project root (.gitignore'd in both repos)

⚠️  Manual steps still required:
  • Add your key to /home/sabbamonte/projects/$ARGUMENTS/.envrc:
      export ANTHROPIC_API_KEY="sk-ant-..."
    then run `direnv allow` in the project root to load it.
    (required — the run fails to start without it)
  • For pushing / opening PRs (not needed for local test runs):
      - add  export PIPELINE_GITHUB_TOKEN="..."  to the same .envrc
      - set repos.codeforge_state.remote and repos.source_code.remote in
        codeforge-state/.codeforge/codeforge.config.yaml
