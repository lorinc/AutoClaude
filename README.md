# Agentic DevOps system — design plan

## Context and goal

This system enables a solo developer to manage 2–3 FastAPI/Python projects simultaneously without manual intervention in the development workflow. The developer operates purely as **PO / BA / QA** — describing goals, reviewing outcomes, and approving merges. All coding, testing, and deployment orchestration is handled autonomously.

The system runs on a dedicated always-on miniPC (Ubuntu Server, no GUI) and integrates with GitHub and GCP.

---

## Core principles

- **One agent at a time.** The orchestrator runs a single Claude Code session across all projects. No parallel agents — keeps costs linear and avoids race conditions.
- **Human gates are minimal and explicit.** You are interrupted exactly three times per task cycle: (1) plan approval before coding starts, (2) stuck signal if the agent cannot self-recover, (3) UAT-ready notification when staging is deployed.
- **Main branch is sacred.** The agent works exclusively on feature/fix branches. Merging to main requires your explicit GitHub PR approval. Production deploy is physically impossible before that.
- **Filesystem-as-brain.** All persistent context lives in structured files within the project repo. Claude loads a file map at session start and fetches only what it needs. No bloated root context.
- **Deterministic over autonomous.** The orchestrator is dumb Python glue. Claude does the thinking. The orchestrator enforces structure, gates, and cost limits — it does not reason.

---

## System components

### Hardware and OS

- MiniPC running **Ubuntu Server** (no GUI — saves RAM and CPU)
- Always-on, accessible over LAN
- Docker (rootless) for sandboxed test and runtime environments
- **Python 3.12** system-wide (orchestrator and all projects use same version)
- Orchestrator venv at `/home/agent/orchestrator-venv`

### Repos

Three separate repos:

| Repo | Purpose |
|---|---|
| `orchestrator` | Scheduler, Telegram bot, project registry, init prompt |
| `project-a` | One repo per project, standard structure |
| `project-b` | One repo per project, standard structure |

The orchestrator repo is cloned onto the miniPC and is the only thing that runs permanently. Project repos are cloned by the orchestrator when registered.

### Cloud

| Service | Role |
|---|---|
| GitHub | Source of truth, branch protection on main, PR-gated merges |
| GCP Cloud Build | CI — runs integration and regression tests on push |
| GCP Cloud Run | Staging UAT environment (ephemeral per PR) |

Cross-project integration: projects interact only via their **GCP staging URLs** — no shared runtime on the miniPC. This keeps local isolation clean.

### Notification and interaction

- **Telegram bot** (in orchestrator repo): notifies on UAT ready, stuck signal, changelog summary. Accepts replies for plan approval and stuck hints.
- **Claude Code CLI**: used directly on the miniPC for agent sessions; also usable for local orchestration commands.
- **GitHub PR interface**: the only place where you approve merges and trigger production deploys.

---

## Multi-project orchestration

The orchestrator maintains a `project_registry.json`:

```json
[
  {
    "name": "project-a",
    "repo_path": "/home/agent/repos/project-a",
    "github_url": "https://github.com/user/project-a",
    "staging_url": "https://project-a-staging.run.app",
    "telegram_chat_id": "123456789",
    "python_version": "3.12",
    "budget_limit_usd": 10.0,
    "common_dependencies": ["fastapi==0.104.1", "pydantic==2.5.0"],
    "last_task": {"slug": "rate-limit", "status": "done", "timestamp": "2026-03-27T14:32:00Z"}
  }
]
```

Fields:
- `common_dependencies`: Enables cross-project consolidation during init. Orchestrator suggests versions already in use.
- `last_task`: Crash recovery and debugging. Tracks most recent activity per project.
- `budget_limit_usd`: Per-project cost control.
- `GCP_PROJECT_ID` is NOT tracked — projects manage via their own `.env` for flexibility.

Task discovery uses two channels:

- **Queued tasks**: markdown spec files dropped into `tasks/pending/` in the project repo. Orchestrator polls on a schedule.
- **Telegram**: a message to the bot with a project name and task description.

**Task priority and blocking**:
- **Ad-hoc task blocking**: If trunk is dirty (unmerged feature branch exists), block new ad-hoc tasks. Record them in `tasks/ideas/` with note "needs clarification - blocked until trunk clean"
- **Merge-first discipline**: Agent works one task at a time per project. Must merge to trunk or revert to last stable trunk before next task starts

**Branch naming**: Orchestrator creates branches as `feature/task-slug`, `fix/task-slug`, or `chore/task-slug`. Type is inferred from task content or defaults to `feature/`.

---

## Filesystem-as-brain structure

Every project repo follows this exact structure. The root `CLAUDE.md` contains **only a file map** — no content. Claude reads it first, then fetches only the files relevant to the current task.

```
project-repo/
│
├── CLAUDE.md                  # ROOT — always loaded. File map only. No detail.
├── status.md                  # Always loaded. Last outcome + next session goal.
├── devlog.md                  # Always loaded via `tail -30`. Append-only log.
├── plan.md                    # Lazy. Epics, decisions, ADRs.
│
├── tasks/
│   ├── pending/               # Orchestrator polls here. Each task = one .md file.
│   ├── active/                # Orchestrator moves task here when agent picks it up.
│   └── done/                  # Orchestrator archives here after session closes.
│
├── src/
│   ├── CLAUDE.md              # Lazy. Cross-cutting codebase concerns (see below).
│   └── module_name/
│       ├── CLAUDE.md          # Lazy. Module scope, conventions, known issues.
│       └── ...
│
└── tests/
    ├── CLAUDE.md              # Lazy. Test conventions, fixture strategy.
    ├── unit/
    │   └── CLAUDE.md          # Lazy. Unit-specific conventions.
    ├── integration/
    │   └── CLAUDE.md          # Lazy. Integration-specific conventions.
    ├── regression/
    │   └── CLAUDE.md          # Lazy. Regression suite conventions.
    └── scratchpad/            # Agent sandbox. Gitignored. Free to write here.
```

### Root `CLAUDE.md` content model

The root file contains:
- Project name and one-line description
- Stack (e.g. Python 3.12 / FastAPI / PostgreSQL)
- Non-default architectural decisions (brief, no explanation — explanation lives in `plan.md`)
- The full file map above, with one-line descriptions of each file's purpose
- The **context loading protocol** (see below) — verbatim, as an instruction block

It must never contain: implementation detail, history, rationale, module descriptions. Those live in their own files.

### Context loading protocol

This is a formal invariant encoded verbatim in every root `CLAUDE.md`. The agent must follow it in order, without skipping steps:

```
1. Read this file (CLAUDE.md) — file map and non-default decisions only.
2. Read status.md — last outcome and next session goal.
3. Run `tail -30 devlog.md` — recent event log for orientation.
4. Read the active task spec from tasks/active/.
5. Load lazy files only as needed for the current task:
   - src/CLAUDE.md if the task touches more than one module
   - src/<module>/CLAUDE.md for each module touched
   - tests/CLAUDE.md and relevant test subdirectory CLAUDE.md
   - plan.md only if the task spec references an ADR or epic
6. Do not load any other files until a failing test requires it.
```

This sequence is the contract. The orchestrator init prompt encodes it. The agent is not permitted to reorder or skip steps.

### `src/CLAUDE.md` content model

Sits one level above modules. Fetched by the agent when a task touches more than one module, or when the task involves cross-module data flow. Skipped for single-module tasks.

Scope — exactly these four things, nothing else:
- **Canonical data shapes** — the Pydantic models or TypedDicts that flow between modules. Not implementation, just the shapes and where they are defined.
- **Cross-module conventions** — naming patterns, error handling strategy, shared utilities and where they live.
- **Inter-project API contracts** — the staging URLs this project calls, what it sends, what it expects back. One entry per dependency.
- **Cross-cutting constraints** — rules that affect more than one module simultaneously (e.g. "all datetimes are UTC and stored as ISO 8601 strings", "never import from a sibling module — go through the service layer").

It must never contain: rationale for decisions (that's `plan.md`), module-specific detail (that's `src/module/CLAUDE.md`), or anything that only one module needs to know.

### `status.md` format

Two fields only. Written by the agent at the end of every session. Read by the agent at the start of the next.

```markdown
## Last outcome
TASK: auth-token-refresh
STATUS: pass
NOTE: had to work around pydantic v2 coercion on datetime fields — see arch/CLAUDE.md#known-issues

## Next session
Implement rate-limiting middleware on /api/v1/* routes. Spec in tasks/pending/rate-limit.md.
```

### `devlog.md` format

Append-only. One structured line per significant event. Designed for `tail -30` to give Claude instant orientation — not for human reading.

```
[2026-03-27 14:32] TASK:auth-refresh OUTCOME:pass NOTE:pydantic v2 datetime coercion workaround applied
[2026-03-27 16:10] TASK:rate-limit OUTCOME:stuck NOTE:cloud build failing on env var injection, awaiting human hint
[2026-03-27 16:45] TASK:rate-limit OUTCOME:pass NOTE:fixed — GCP secret manager ref syntax was wrong
```

Pattern: `[ISO-datetime] TASK:slug OUTCOME:pass|fail|stuck NOTE:one-line-summary`

### Task spec file format (`tasks/pending/task-slug.md`)

```markdown
# Task: rate-limit-middleware

## Goal
Add per-IP rate limiting on all /api/v1/* routes.

## Context and constraints
- Use slowapi (already in requirements.txt)
- Limits: 100 req/min for authenticated, 20 req/min for anonymous
- Do not touch auth middleware

## My concerns
- Redis dependency — we don't have Redis in staging yet. Use in-memory for now, document the gap.

## Acceptance criteria
- Unit tests pass
- Integration test: 21st request from same IP returns 429
- No regression on existing auth tests
```

---

## Development lifecycle (per task)

1. **You write the task spec** — in conversation with Claude on your laptop. Claude criticises, proposes best practices, you decide. Output is a `tasks/pending/task-slug.md` file committed to the repo (or sent via Telegram for urgent).

2. **Orchestrator runs Definition of Ready check** — before picking up the task, the orchestrator verifies:
   - `tasks/pending/task-slug.md` has all four required sections (Goal, Context and constraints, My concerns, Acceptance criteria)
   - `main` branch CI is green (last Cloud Build status via GCP API)
   - No uncommitted changes on the working branch for this project
   - No open PR already exists for this task slug
   If any check fails, the task is held in `pending/` and you are notified via Telegram with the specific reason. The session does not start.

3. **Orchestrator picks up the task** — moves spec to `tasks/active/`, creates the feature branch, starts a Claude Code session with the correct project context.

4. **Agent reads context** — follows the context loading protocol exactly: root `CLAUDE.md` → `status.md` → `tail -30 devlog.md` → task spec → lazy files as needed.

5. **Agent sends plan via Telegram** — before touching any production code. You approve or adjust by reply.

6. **Agent codes on a feature branch** — TDD loop: failing test first, then implementation. Scratchpad isolation for heuristic debugging. Never edits main.

7. **Local tests pass → refactor review** — a separate isolated Claude API call (high temperature, "senior architect" system prompt) reviews the diff, strips debug artifacts, squashes commits.

8. **Push branch → CI** — GCP Cloud Build runs integration and regression tests.

9. **CI passes → staging deploy** — Cloud Run spins up a preview instance. Telegram sends you the URL + a changelog summary (generated by the agent from `git log --oneline feature-branch ^main`, fallback to `devlog.md` if commits unclear. Concise prose for UAT context).

10. **You do UAT** — test in browser. When satisfied, approve and merge the PR on GitHub.

11. **Merge triggers prod deploy** — automatic. No further agent involvement.

12. **Agent closes session** — writes `status.md`, appends to `devlog.md`, moves task to `tasks/done/`. Orchestrator picks up the next queued task.

**Session recovery**: If miniPC reboots mid-session, orchestrator detects incomplete task and alerts you via Telegram. You and orchestrator recover together manually (not automated).

### Observability and verification

The agent must verify its own deployments before declaring success. This prevents blind handoffs and enables autonomous error detection.

**Post-deploy verification sequence** (runs once after staging deploy, before UAT notification):

1. **Health check** — `curl {staging_url}/health` with 30s timeout. If non-200 or timeout: fail immediately, skip remaining checks.
2. **Log scan** — fetch last 50 lines from Cloud Run via `gcloud run services logs read --limit=50 --format=json`. Parse for `severity: ERROR|CRITICAL`. If found: include in Telegram alert.
3. **Smoke test** — run Playwright against staging URL. Single test file: `tests/smoke/staging_smoke.py`. Max 3 critical paths (e.g. homepage, API docs, one authenticated endpoint). 60s timeout total. Capture screenshots to `tests/screenshots/staging-{timestamp}/`. If test fails: attach screenshot links to Telegram alert.

**Token conservation**:
- Verification runs **outside** the agent session — orchestrator executes these as shell commands after session closes.
- Agent does NOT read logs or screenshots unless verification fails and you request debug.
- No LLM calls during verification — pure bash/Python scripts.

**Integration with stuck recovery**:
- Verification failure is treated as a **stuck signal** — same as turn limit or CI failure.
- On verification failure: (1) commit WIP to branch with verification logs as code comments, (2) append to `devlog.md`: `[timestamp] TASK:slug OUTCOME:stuck-retry-N NOTE:staging verification failed: {error summary}`, (3) increment retry counter, (4) if retry < 3: new session with clean context + WIP branch + verification error context, (5) if retry = 3: Telegram alert.
- Agent can autonomously retry up to 3 times, using verification errors as debugging context.
- This maintains autonomous recovery while preventing infinite loops.

**Cost cap**:
- Smoke tests excluded from agent context via `tests/smoke/CLAUDE.md` containing only: "Smoke tests are for deployment verification only. Do not load unless task spec explicitly references smoke test changes."
- Agent writes smoke tests during task implementation but skips this directory in future sessions unless directed.
- Screenshots stored locally, never uploaded to LLM context.
- Pre-deploy smoke tests (see below) run locally before push — zero cloud cost, catches deployment issues before CI.

**Project registry additions**:
```json
{
  "health_check_path": "/health",
  "smoke_test_file": "tests/smoke/staging_smoke.py",
  "max_smoke_duration_seconds": 60
}
```

**Mandatory in every project**:
- `/health` endpoint (returns `{"status": "ok"}` if app is functional)
- `tests/smoke/staging_smoke.py` (Playwright test against staging URL, max 3 assertions)
- `tests/smoke/local_smoke.py` (optional but recommended — Playwright test against `localhost:8000`, runs before push as pre-commit hook, catches deployment config issues early)

**Verification failure modes**:
- Health check fails → Telegram: "Staging deploy failed health check. Logs: {last 10 error lines}"
- Smoke test fails → Telegram: "Smoke test failed. Screenshot: {path}. Error: {playwright error message}"
- Logs contain errors → Telegram: "Staging deployed but errors detected: {error summary}"

**Pre-deploy smoke test** (optional, runs before step 8 push):
- `tests/smoke/local_smoke.py` runs against local dev server via pre-push git hook
- Validates: app starts, health endpoint responds, critical routes return expected status codes
- If fails: push is blocked, agent sees error in terminal output, can debug locally
- Bridges gap between "tests pass" and "deployment works" — catches missing env vars, broken imports, config issues
- Zero cloud cost, fast feedback (runs in <10s)

Post-deploy verification happens in step 9. If any check fails, stuck recovery protocol triggers (max 3 retries with verification logs as context).

### Quality gates

- **Definition of Ready gate**: the orchestrator enforces four preconditions before any session starts — complete task spec, green CI on main, clean working branch, no duplicate open PR. A failed gate costs zero tokens and prevents a whole class of wasted sessions.
- **Scratchpad isolation**: during heuristic debugging, agent is confined to `tests/scratchpad/`. Production code is read-only until a failing test exists.
- **Refactor hook**: mandatory before any commit. Removes debug prints, redundant logic, trial-and-error commits.
- **Turn limit**: max 15 tool-calls per sub-task before the orchestrator sends a stuck signal to Telegram and pauses.
- **Stuck recovery**: Max 3 retries per task. On turn limit: (1) commit WIP to branch with lessons learned in-branch (code comments, scratchpad notes, attempt log), (2) append to `devlog.md` on trunk: `[timestamp] TASK:slug OUTCOME:stuck-retry-N NOTE:attempt reverted, lessons: <summary>`, (3) increment retry counter in `tasks/active/{task-slug}.retry`, (4) if retry < 3: new session with clean context + WIP branch, (5) if retry = 3: Telegram alert with minimal summary. Lessons preserved permanently in devlog on trunk.
- **Budget cap**: `max_budget_usd` set per session in the Claude SDK. Orchestrator kills the session if exceeded and notifies you.

---

## Security and cost governance

- **Dependency pinning**: `pip install --require-hashes` with verified `requirements.txt`. No large wrappers (no LiteLLM) — direct Anthropic Python SDK only.
- **GCP Service Account**: Separate service accounts per responsibility: (1) `orchestrator-sa` with `roles/cloudbuild.builds.viewer` for CI status checks, (2) `ci-builder-sa` with `roles/cloudbuild.builds.editor` for Cloud Build, (3) `deployer-sa` with `roles/run.admin` for Cloud Run deployments. Each scoped to specific GCP projects only.
- **Secrets management**: Orchestrator uses `.env` file on miniPC (Anthropic API key, Telegram bot token, GCP service account keys). Each project has its own `.env` for project-specific secrets including `GCP_PROJECT_ID`. Never commit secrets to repos.
- **Docker rootless**: no root access from within agent containers.
- **Branch protection**: GitHub main branch requires PR + passing CI. No force-push. No direct push from the agent.
- **Cost logging**: orchestrator logs token usage and USD cost per session to a `costs.jsonl` file. Weekly summary sent via Telegram.

---

## Project initiation ritual

A new project goes from idea to agent-ready in approximately 20 minutes, entirely via a single laptop conversation.

### Flow

1. You open a Claude conversation and say: *"I want to start a new project. Use the initiation prompt."*
2. Claude (loaded with `init_prompt.md` from the orchestrator repo) interviews you: stack confirmation, integrations with existing projects' staging APIs, non-default architectural choices, first epic.
3. Claude generates the full scaffold: all `CLAUDE.md` files, `plan.md`, `status.md` (blank template), `devlog.md` (seeded with init entry), `tasks/pending/` (first task spec).
4. You review the output in the conversation, adjust anything, then commit and push to a new GitHub repo.
5. You add one entry to `project_registry.json` in the orchestrator repo and push.
6. Orchestrator detects the new registry entry, clones the project repo, and confirms via Telegram: *"Project X registered. 1 task pending."*

### `init_prompt.md` (lives in orchestrator repo)

The initiation system prompt instructs Claude to:
- Act as a senior FastAPI architect
- Ask about: existing project integrations (by staging URL), non-mainstream stack choices, data model shape, first epic scope
- Criticise any choices that conflict with known constraints before generating
- Emit the full scaffold as a series of clearly delimited files, ready to be committed as-is
- Embed the context loading protocol verbatim in the root `CLAUDE.md` it generates
- Seed `devlog.md` with: `[ISO-datetime] TASK:init OUTCOME:pass NOTE:project scaffolded`
- Seed `status.md` with: next session = first task slug

---

## Build sequence (recommended order)

This is the order in which to build the system, designed so each step is independently useful:

1. **Ubuntu Server setup** on the miniPC — SSH, Docker rootless, Python venv, Claude Code CLI, `gcloud`, `gh`.
2. **First project repo** — manually create the filesystem structure above for one existing project. Write the `CLAUDE.md` files by hand the first time to understand the conventions.
3. **Python orchestrator — core loop** — task discovery (polls `tasks/pending/`), Definition of Ready check, branch management, Claude Code session launch, test gate. No Telegram yet.
4. **Refactor review hook** — separate Claude API call post-test-pass, pre-commit.
5. **GCP Cloud Build + Cloud Run** — CI pipeline and staging deploy on push.
6. **Telegram bot** — notifications (UAT ready, stuck, changelog). Then add reply-handling for plan approval and stuck hints.
7. **Cost governor** — budget cap, turn limit, `costs.jsonl` logging, weekly Telegram summary.
8. **Multi-project support** — `project_registry.json`, scheduler queue, Telegram urgent path.
9. **`init_prompt.md`** — the initiation conversation prompt that generates full scaffolds.
10. **Orchestrator as its own repo** — clean it up, document it, make it cloneable.

---

## TBD — what needs to be designed and built before the orchestrator repo is clone-and-go

The following items are not yet designed in sufficient detail to implement. Each needs a focused session before or during build.

### Design gaps (need decisions before building)

1. [ ] **`init_prompt.md` full content** — DEFERRED to dedicated session. System prompt for project initiation. Must encode filesystem conventions, interview questions, output format.
2. [ ] **Root `CLAUDE.md` template** — DEFERRED to dedicated session. Template with context loading protocol verbatim, file map, minimal entry point design.
3. [x] **Orchestrator `project_registry.json` full schema** — RESOLVED. Schema:
   ```json
   {
     "name": "project-a",
     "repo_path": "/home/agent/repos/project-a",
     "github_url": "https://github.com/user/project-a",
     "staging_url": "https://project-a-staging.run.app",
     "telegram_chat_id": "123456789",
     "python_version": "3.12",
     "budget_limit_usd": 10.0,
     "common_dependencies": ["fastapi==0.104.1", "pydantic==2.5.0"],
     "last_task": {"slug": "rate-limit", "status": "done", "timestamp": "2026-03-27T14:32:00Z"}
   }
   ```
4. [x] **Task priority model** — RESOLVED. Block ad-hoc tasks until trunk clean:
   - **Ad-hoc task blocking**: If trunk is dirty (unmerged feature branch exists), block new ad-hoc tasks. Record them in `tasks/ideas/` with note "needs clarification - blocked until trunk clean".
   - **Merge-first discipline**: Agent works on one task at a time per project. Must merge to trunk or revert to last stable trunk before next task starts.
5. [x] **Stuck recovery protocol** — RESOLVED. Max 3 retries, then alert:
   - On turn limit hit:
     1. Commit WIP to branch with all lessons learned in-branch (code comments, scratchpad notes, attempt log)
     2. Append to `devlog.md` on trunk: `[timestamp] TASK:slug OUTCOME:stuck-retry-N NOTE:attempt reverted, lessons: <one-line summary>`
     3. Increment retry counter in `tasks/active/{task-slug}.retry` file
     4. If retry count < 3: start new session with clean context + WIP branch
     5. If retry count = 3: alert via Telegram with short summary (what was attempted, current state, branch name for review)
   - **Lessons preservation**: When branch is reverted, ALL lessons are appended to `devlog.md` on trunk before revert. One-line format (captured by `tail -30`) must contain all valuable knowledge from the attempt.
   - **No over-engineering**: Summary is minimal — just enough for you to decide next action (continue with hint, abandon, or manual takeover).
6. [ ] **Refactor review prompt** — DEFERRED. Too vague and non-deterministic. Risk of infinite loops. Design this later when core orchestrator is stable.
7. [x] **Changelog generation** — RESOLVED. Simple prose from git commits:
   - Primary source: `git log --oneline feature-branch ^main` (commit messages)
   - Fallback: if commits are unclear, read `devlog.md` last entry for this task
   - Format: Prose summary, not bullet points. Just enough context for UAT ("Added rate limiting to API endpoints. 100 req/min for authenticated users, 20 for anonymous.")
   - Sent in Telegram UAT notification with staging URL
   - No over-engineering — concise, human-readable, task-focused.

### Build tasks (implementation, not design)

- [ ] Ubuntu Server setup script — idempotent bash script that provisions a fresh miniPC from zero: packages, Docker rootless, Python, Claude Code CLI, GCP auth, SSH config, Playwright.
- [ ] Orchestrator repo scaffold — directory structure, `README.md`, `project_registry.json` (empty), `costs.jsonl` (empty), `init_prompt.md` (placeholder).
- [ ] Python orchestrator — `scheduler.py`, `task_runner.py`, `telegram_bot.py`, `cost_governor.py`. Each as a standalone module.
- [ ] Definition of Ready checker — `readiness_check.py` in the orchestrator. Validates task spec structure using Pydantic model, queries GCP Cloud Build API for CI status, checks git branch state, queries GitHub API for open PRs. Returns a structured result; orchestrator acts on it.
- [ ] Post-deploy verification module — `verify_deploy.py` in orchestrator. Runs health check, log scan, smoke test. Returns structured result (pass/fail + error details). No retries, no LLM calls. Max 90s total execution time.
- [ ] Devlog edit hook — Git hook that enforces append-only writes to `devlog.md`. Rejects commits that delete or modify existing lines. Lives in orchestrator repo, symlinked into each project's `.git/hooks/`.
- [ ] Pre-push Git hook script — triggers refactor review before any branch push. Lives in the orchestrator repo, symlinked into each project's `.git/hooks/`.
- [ ] GCP Cloud Build `cloudbuild.yaml` template — standard template for FastAPI projects. Parameterised for project name and staging URL.
- [ ] Cloud Run deploy template — standard `service.yaml` for staging environments.
- [ ] Telegram bot setup guide — BotFather flow, webhook vs polling decision, chat ID retrieval.
- [ ] Smoke test templates — Two files: (1) `tests/smoke/staging_smoke.py` for post-deploy verification (max 3 assertions, 60s timeout), (2) `tests/smoke/local_smoke.py` for pre-push validation (runs against localhost:8000, <10s). Both use Playwright.
- [ ] Smoke test CLAUDE.md — `tests/smoke/CLAUDE.md` containing single line: "Smoke tests are for deployment verification only. Do not load unless task spec explicitly references smoke test changes." This excludes the directory from agent context by default.
- [ ] Pre-push smoke test hook — Git hook that runs `tests/smoke/local_smoke.py` before allowing push. Blocks push if test fails. Lives in orchestrator repo, symlinked into each project's `.git/hooks/`.
- [ ] Health endpoint template — `/health` FastAPI route template. Returns JSON `{"status": "ok", "timestamp": "ISO8601"}`. Add to project init scaffold.
- [ ] First real project migration — take an existing project, apply the filesystem structure, add health endpoint and smoke test, run one task end-to-end manually with verification, then hand off to the orchestrator.
- [ ] `costs.jsonl` weekly summary script — reads the log, formats a Telegram message, scheduled via cron.

### Open questions — RESOLVED

1. [x] **Branch naming convention** — Use `feature/task-slug`, `fix/task-slug`, `chore/task-slug`. Task spec does NOT need a type field (keep it simple). Orchestrator infers type from task content or defaults to `feature/`.
2. [x] **Python version and venv strategy** — Orchestrator uses same Python version as projects (Python 3.12). Single venv at `/home/agent/orchestrator-venv`.
3. [x] **GCP project structure** — Projects can share a GCP project. Each project's `.env` contains `GCP_PROJECT_ID` for flexibility. Orchestrator does NOT need to track GCP project ID in registry — projects manage this themselves.
4. [x] **Session resume behaviour** — On miniPC reboot mid-session, orchestrator alerts you via Telegram. You and orchestrator recover the session together (manual intervention, not automated).
5. [x] **Secrets management** — `.env` file on miniPC for orchestrator secrets (Anthropic, Telegram, GCP). Each project has its own `.env` for project-specific secrets (including `GCP_PROJECT_ID`).
6. [x] **Log retention** — `devlog.md` grows indefinitely. No rotation, no archiving. Agent always reads `tail -30`.

---

*Document generated from a planning conversation. Continue in a fresh session by sharing this file and saying: "Continue the agentic DevOps system design. Here's where we left off."*
