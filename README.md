# CodeForge

CodeForge is an AI-driven software development tool. You give it a one-sentence brief
and it runs a multi-agent pipeline — requirements, architecture, coding, test design,
sandboxed test execution, code review, and security review — then commits the result and
opens a pull request. Every run is gated, retried, and resumable: when an agent is
low-confidence or a loop exhausts its retries, the run *escalates* to a human instead of
guessing.

This README covers getting a fresh machine set up and running your first project.

---

## How it works (1-minute tour)

A run drives seven agents through a state machine:

| Phase | Agent | What it does |
|-------|-------|--------------|
| 1 | Requirements analyst | Turns the brief into acceptance criteria; asks for clarification if unsure |
| 2 | Architecture designer | Locks tech decisions and a design within your stack |
| 3 | Coder | Emits the source (whole files for new projects, surgical diffs for continuations) |
| 4 | Test designer | Writes the test suite |
| 5 | Test runner | Runs the tests in a **Docker sandbox** |
| 6 | Code reviewer | Reviews the diff; loops back to the coder on findings |
| 7 | Security reviewer | Security pass; loops back on findings |

On success CodeForge commits to two repos — a **state repo** (run logs, decisions, project
state) and your **source repo** — and opens a PR on the source remote. Retry limits,
confidence thresholds, and a global call ceiling are all configurable per project.

Each project lives in its own directory and has its own config; the CodeForge install
itself is just the engine.

---

## Prerequisites

- **Python 3.12** (the package requires `>=3.12`)
- **Docker** — the test runner executes the generated tests in a container. The daemon
  must be running and you must be able to pull images.
- **Git**
- An **Anthropic API key**
- A **GitHub token** (`PIPELINE_GITHUB_TOKEN`) — required by the runner; used to open PRs
- *(Optional but recommended)* [`direnv`](https://direnv.net/) for per-project env vars

---

## Setup

### 1. Clone and create a virtualenv

```bash
git clone <this-repo-url> codeforge
cd codeforge
python3.12 -m venv .venv
source .venv/bin/activate
```

### 2. Install

```bash
pip install -e .
```

This installs the `codeforge` CLI into your venv. Verify:

```bash
codeforge --help
```

### 3. Provide credentials

CodeForge reads two environment variables at run time — **both are required**:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export PIPELINE_GITHUB_TOKEN="ghp_..."      # used to open PRs
```

You can keep these in a project-local `.envrc` (with direnv) or a `.env` file you source.
A convenience `activate.sh` is gitignored; a typical one looks like:

```bash
#!/bin/bash
cd ~/codeforge
source .venv/bin/activate
set -a && source .env && set +a
```

> `.env` and `activate.sh` are gitignored — never commit secrets.

### 4. Pull a sandbox image

The test runner needs the stack's Docker image present locally (it does **not** build one):

```bash
docker pull python:3.12-slim       # for the python stack
docker pull node:20-bookworm       # for the nextjs-supabase stack
```

---

## Creating a project

Each project is two git repos plus a config. The fastest way to scaffold one is the
**`/new-project` Claude Code skill** (run from inside this repo in Claude Code):

```
/new-project my-app                 # defaults to the python stack
/new-project my-app nextjs-supabase
```

It creates the directory layout, copies the default config, seeds the stack's locked tech
decisions, scaffolds a working source base, and initializes both git repos on `main`.
Supported stacks: **`python`**, **`nextjs-supabase`**.

### Or scaffold manually

```bash
codeforge init --project-dir /path/to/my-app/codeforge-state
```

This writes `.codeforge/codeforge.config.yaml` with a template you must fill in. At minimum
you need to set:

- `stack.profile` — `"python"` or `"nextjs-supabase"`
- `test_runner.sandbox_image` — e.g. `python:3.12-slim` or `node:20-bookworm`
- the `repos:` block — paths and remotes for the state repo and the source repo

A project directory layout looks like:

```
my-app/
├── codeforge-state/          # the "project directory" you pass to --project-dir
│   ├── .codeforge/
│   │   └── codeforge.config.yaml
│   ├── project-state/        # tech decisions, accumulated state
│   └── run-logs/             # per-run events, briefs, artifacts
└── source/                   # the actual code repo CodeForge writes to
```

The project config is **deep-merged over** the installation defaults
([codeforge/config/codeforge.config.yaml](codeforge/config/codeforge.config.yaml)), so you
only set what differs.

---

## Running

All commands take `--project-dir` (`-d`) pointing at the directory that contains
`.codeforge/` (the `codeforge-state/` dir above).

### Start a run

```bash
codeforge run \
  -d /path/to/my-app/codeforge-state \
  -b "Build a CLI todo app with add, list, and complete commands" \
  --run-mode new_project
```

**Run modes:**

- `new_project` *(default)* — greenfield build; the brief describes the whole project.
- `continuation` — add a feature to an existing repo. Agents get read-only tools to search
  and read the current source, and changes are applied as surgical diffs. The brief
  describes the feature to add.

On success you'll see the run ID, the two commit SHAs, and the PR URL.

### When a run escalates

If an agent is low-confidence or a retry loop is exhausted, the run stops and escalates:

```
Codeforge escalated: <reason>
Run ID: run-abc123
Review run-logs/run-abc123/events.jsonl for details.
```

Inspect the logs, then resume — CodeForge re-enters at the right state and prompts you to
approve, modify, or reject:

```bash
codeforge resume run-abc123 -d /path/to/my-app/codeforge-state
```

A run is single-flight per project (a lock prevents concurrent runs on the same directory).

---

## Configuration reference

Key sections in `codeforge.config.yaml` (see the
[annotated default](codeforge/config/codeforge.config.yaml) for the full list):

- `stack.profile` — target tech stack
- `retry_limits` — per-loop retry budgets (code review, security, test, etc.)
- `global_ceiling.max_agent_calls_per_run` — hard cap on total agent calls
- `confidence_thresholds` — per-agent minimum confidence before escalating
- `test_runner` — `sandbox_image`, `timeout_seconds`, `environment_vars`
- `agents.<name>` — per-agent model, temperature, max_tokens, thinking budget
- `repos` — state repo + source repo paths, remotes, branch prefix, PR target,
  `auto_merge`

### nextjs-supabase note

The Supabase stack also needs these wired into `test_runner.environment_vars` and present
in your environment (tests mock Supabase, so they pass without a live project, but runtime
needs them):

```
NEXT_PUBLIC_SUPABASE_URL
NEXT_PUBLIC_SUPABASE_ANON_KEY
SUPABASE_SERVICE_ROLE_KEY
```

---

## Developing CodeForge itself

```bash
pytest            # run the test suite
ruff check .      # lint
mypy .            # type-check (strict)
```

---

## Troubleshooting

- **`Required environment variable(s) not set`** — export `ANTHROPIC_API_KEY` and
  `PIPELINE_GITHUB_TOKEN` before running.
- **`Cannot connect to Docker daemon`** — start Docker; ensure your user can talk to it.
- **`Sandbox image not found`** — `docker pull` the image named in
  `test_runner.sandbox_image`.
- **`The 'repos' block must be set`** — fill in the `repos:` block in your project config.
- **A run is stuck / won't start** — another run may hold the project lock; check for an
  in-flight `codeforge run` on the same `--project-dir`.
