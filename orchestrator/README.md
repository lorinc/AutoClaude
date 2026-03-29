# `orchestrator/` — Module Contracts

Each file's contract: purpose, main path, error/alternative paths, defaults, required inputs, optional inputs, and outputs.

---

## `__main__.py`

**Purpose:** Entry point. Wires together all subsystems and starts the scheduler.

**Main path:**
1. Load `.env` from repo root (silently skips if absent).
2. Configure loguru: INFO on stderr, rotating DEBUG log at `orchestrator.log`.
3. Fail-fast: exit 1 if `project_registry.json` is missing or `ANTHROPIC_API_KEY` is unset.
4. Build `StateStore`, run `FileWatcher.scan()` for each project (startup adoption).
5. Optionally start `TelegramCommandDispatcher` as a daemon thread.
6. Start `Scheduler.run()` (blocking). On exit, stop Telegram dispatcher and close DB.

**Error paths:**
- `project_registry.json` missing → logs error with path to `.example.json`, `sys.exit(1)`.
- `ANTHROPIC_API_KEY` unset → logs error, `sys.exit(1)`.
- `SIGTERM` received → sets `Scheduler._shutdown = True`; exits after current task finishes.

**Obligatory env / files:**
- `project_registry.json` at repo root.
- `ANTHROPIC_API_KEY` env var (or in `.env`).

**Optional env vars (with defaults):**

| Variable | Default | Effect |
|---|---|---|
| `ORCHESTRATOR_ENV` | `dev` | Tag in startup log only |
| `DRY_RUN` | `false` | Pass-through to `Scheduler` |
| `POLL_INTERVAL` | `60` | Seconds between scheduler polls |
| `TURN_LIMIT` | `15` | Max tool-use turns per session |
| `MAX_RETRIES` | `3` | Max explore/fix cycles before escalation |
| `HANDOFF_SHELL` | `fish` | Shell for tmux handoff sessions |
| `TELEGRAM_BOT_TOKEN` | — | Enables Telegram; skipped if absent |
| `TELEGRAM_CHAT_ID` | — | Enables Telegram; skipped if absent |

**Optional files:**
- `.env` at repo root — loaded via `python-dotenv` before env var reads.
- `init_prompt.md` at repo root — appended as system prompt to every session; empty string if absent.

**Outputs:** None (side-effects only: logging, subprocess execution).

---

## `__init__.py`

**Purpose:** Package marker. Empty.

---

## `state_store.py`

**Purpose:** SQLite-backed, thread-safe task state machine. Single source of truth for task lifecycle. Prevents double-dispatch and lost-task bugs.

**State machine:**

```
PENDING → EXPLORE → FIXING → PASSED
                           ↘ EXPLORE (retry)
                    ↘ ESCALATED
         ↘ PASSED
         ↘ FAILED
         ↘ ESCALATED
         ↘ CANCELLED  (from any non-terminal state)
```

Terminal states: `PASSED`, `FAILED`, `ESCALATED`, `CANCELLED`.

Dispatch priority (for `next_task`): `FIXING > EXPLORE > PENDING`.

**Main path:**
1. `StateStore(db_path)` — opens/creates SQLite with WAL mode.
2. `add_task(project, slug, spec_path)` — inserts PENDING; no-op if slug already exists.
3. `transition(project, slug, from_state, to_state, **updates)` — atomic CAS update.
4. `next_task(project)` — returns oldest actionable task by priority.
5. `close()` — closes DB connection.

**Error paths:**
- `add_task` with duplicate `(project, slug)` → returns `False` (no exception).
- `transition` when task is not in `from_state` → returns `False` (no exception). Caller decides what to do.
- `transition` with unknown column in `**updates` → raises `ValueError`.

**Obligatory inputs:**
- `db_path: Path | str` — path to SQLite file (use `":memory:"` for tests).
- `add_task`: `project`, `slug`, `spec_path` (strings).
- `transition`: `project`, `slug`, `from_state`, `to_state`.

**Optional inputs:**
- `add_task`: `model: str | None` — Claude model ID to use for this task.
- `transition` kwargs (only these columns are allowed): `branch`, `retry_n`, `explore_guide`, `cost_usd`, `model`.

**Outputs:**
- `add_task` → `bool` (True = inserted).
- `transition` → `bool` (True = updated).
- `cancel_task` → `bool` (True = cancelled).
- `next_task` → `Task | None`.
- `get_task` → `Task | None`.
- `list_tasks` → `list[Task]`.

---

## `scheduler.py`

**Purpose:** Main poll loop. Reads the project registry, syncs filesystem → DB, picks the next actionable task per project, and dispatches to the correct session runner. Owns all state transitions.

**`ProjectConfig` fields (loaded from `project_registry.json`):**

| Field | Required | Default | Description |
|---|---|---|---|
| `name` | yes | — | Project identifier |
| `repo_path` | yes | — | Absolute path to target repo |
| `github_url` | yes | — | GitHub `owner/repo` used for `gh` CLI calls |
| `staging_url` | no | `""` | UAT staging URL (informational) |
| `telegram_chat_id` | no | `""` | Per-project override (unused by core loop currently) |
| `python_version` | no | `"3.12"` | Informational |
| `budget_limit_usd` | no | `10.0` | Total USD budget per task |
| `explore_budget_fraction` | no | `0.3` | Fraction of budget for explore sessions |

**Main path (per poll cycle):**
1. Reload registry (hot-reload on every cycle).
2. For each project: `_sync_fs_to_db()` → pick `next_task()`.
3. Route to `_dispatch_primary`, `_dispatch_explore`, or `_dispatch_fix` based on task state.
4. Call `task_runner` session function → receive `SessionResult`.
5. Log cost to `costs.jsonl`, accumulate in DB.
6. Transition task state based on outcome.
7. Sleep `poll_interval` seconds.

**Dispatch outcomes → state transitions:**

| Session | Outcome | Next State | Side-effects |
|---|---|---|---|
| Primary | `pass` | `PASSED` | `push_branch`, move task `active→done` |
| Primary | `fail` | `FAILED` | move task `active→done` |
| Primary | `stuck` | `EXPLORE` | `commit_wip`, `promote_devlog_to_main` |
| Explore | guide produced | `FIXING` | guide stored in DB |
| Explore | no guide | `ESCALATED` | `notify_stuck`, move task `active→done` |
| Fix | `pass` | `PASSED` | `push_branch`, move task `active→done` |
| Fix | `stuck`/`fail`, retries left | `EXPLORE` | `commit_wip`, `promote_devlog_to_main`, clear guide |
| Fix | `stuck`/`fail`, max retries | `ESCALATED` | `notify_stuck`, move task `active→done` |

**Error paths:**
- Registry absent → `load_registry` raises `FileNotFoundError` (unhandled; crashes scheduler loop).
- `SIGTERM` → `_shutdown = True`; loop exits after current task.
- No actionable tasks for a project → log debug, skip.

**Obligatory inputs (constructor):**
- `registry_path`, `init_prompt`, `turn_limit`, `max_retries`, `poll_interval`, `dry_run`.

**Optional inputs (constructor):**
- `state_store` — defaults to in-memory SQLite if omitted.
- `notify_stuck` — defaults to a logger-only stub if omitted.
- `costs_path` — defaults to `Path("costs.jsonl")`.

**Outputs:** None (drives side-effects in `task_runner`, `git_manager`, `state_store`).

---

## `task_runner.py`

**Purpose:** Executes Claude Code subprocesses. Provides three session runners (`run_primary_session`, `run_explore_session`, `run_fix_session`) built on the core `run_session`. Does not touch state — that is the scheduler's responsibility.

**Session types (set via `AUTOCLAUDE_SESSION_TYPE` env var for the agent to read):**
- `primary` — first attempt at a task.
- `explore` — sandbox exploration on a throwaway branch.
- `fix` — targeted fix guided by the explore guide.

**`run_session` main path:**
1. Spawn `claude -p --dangerously-skip-permissions --verbose --max-budget-usd ... --output-format stream-json` as a subprocess with `start_new_session=True`.
2. Stream stdout line-by-line; count tool-use blocks to track turns.
3. If `turn_count >= turn_limit`: SIGTERM the process group → outcome `stuck`.
4. Wait up to 30s for exit; SIGKILL if it hangs.
5. Parse final `result` event from stream for `total_cost_usd`.
6. Return `SessionResult(outcome, turn_count, exit_code, note, cost_usd)`.

**`run_primary_session`:**
- Creates branch, moves task `pending→active`, runs session, appends devlog.

**`run_explore_session`:**
- Checks out feature branch, creates sandbox branch, runs session.
- Reads and deletes `tasks/active/{slug}.guide.md` if written by the agent.
- Returns to feature branch, deletes sandbox branch.
- Returns `(SessionResult, guide_content_or_None)`.

**`run_fix_session`:**
- Checks out feature branch, prepends explore guide to system prompt if present, runs session.

**`infer_branch_type(slug)`:**
- Returns `"fix"` if slug contains fix/bug/patch/hotfix keywords.
- Returns `"chore"` if slug contains chore/cleanup/refactor/deps/lint/format/bump keywords.
- Defaults to `"feature"`.

**Error paths:**
- Process group kill raises `ProcessLookupError` → silently ignored (process already exited).
- Process hangs after 30s → SIGKILL, then `process.wait()`.
- `run_explore_session`: sandbox branch deletion fails → logs warning, does not raise.
- `dry_run=True` in `run_primary_session`: logs the invocation, skips subprocess, moves task to `done`, returns fake `pass` result.

**Obligatory inputs:**
- All runner functions: `repo_path`, `task_slug`, `budget_usd`, `turn_limit`.
- Primary: additionally `branch_name`, `init_prompt`, `dry_run`.
- Explore: additionally `feature_branch`, `sandbox_branch`, `explore_prompt`.
- Fix: additionally `feature_branch`, `base_prompt`, `explore_guide`.

**Optional inputs:**
- `model` — all runners default to `"claude-sonnet-4-6"`.
- `explore_guide` in `run_fix_session` — if `None`, no guide is prepended.

**Outputs:**
- `run_primary_session` → `SessionResult`.
- `run_explore_session` → `tuple[SessionResult, str | None]`.
- `run_fix_session` → `SessionResult`.

**Side-effects:**
- Moves task spec `.md` files between `tasks/pending/`, `tasks/active/`, `tasks/done/`.
- Appends to `devlog.md`.
- Git branch operations (via `git_manager`).

---

## `cost_governor.py`

**Purpose:** Appends one JSON line to `costs.jsonl` after every completed session. Pure I/O — no state, no decisions.

**Main path:** `log_cost(costs_path, build_cost_record(...))` — builds record dict, opens file in append mode, writes one JSON line.

**Error paths:** None handled. `open()` failure propagates to caller.

**Obligatory inputs:**
- `build_cost_record`: `project`, `task`, `model`, `outcome`, `turns`, `cost_usd`.
- `log_cost`: `costs_path: Path`, `record: dict`.

**Outputs:** None (writes to `costs.jsonl`). File is created if absent.

**Record schema:**
```json
{"ts": "ISO8601", "project": "...", "task": "...", "model": "...", "outcome": "...", "turns": 0, "cost_usd": 0.0}
```

---

## `readiness_check.py`

**Purpose:** Definition of Ready gate. Runs four checks before any primary session. Reports only — never mutates state.

**Checks:**

| Check | Pass condition |
|---|---|
| `check_task_spec` | Spec file contains all four required section headers |
| `check_branch_clean` | No uncommitted changes; no unmerged agent branches (`feature/`, `fix/`, `chore/`) |
| `check_no_duplicate_pr` | No open GitHub PR already exists for any `{prefix}/{slug}` branch |
| `check_ci_green` | Last completed GitHub Actions run on `main` has `conclusion == "success"` |

Required spec sections: `## Goal`, `## Context and constraints`, `## My concerns`, `## Acceptance criteria`.

**`run(spec_path, repo_path, github_url, task_slug)`:** Runs all four checks, returns `ReadinessResult`. `ready` is `True` only if all four pass.

**`parse_task_model(spec_path)`:**
- Reads optional `## Model` section (values: `haiku`, `sonnet`, `opus`).
- Falls back to slug-based inference: `architect`/`design` → opus, chore keywords → haiku, else → sonnet.

**Error paths:**
- Spec file not found → `task_spec` fails with reason.
- `git status` / `git branch` failure → `branch_clean` fails with stderr.
- `gh pr list` / `gh run list` failure → respective check fails with stderr.
- `gh run list` returns no runs → `ci_green` passes with reason `"No CI runs on main yet"`.

**Obligatory inputs:**
- `run`: `spec_path: Path`, `repo_path: Path`, `github_url: str`, `task_slug: str`.
- `parse_task_model`: `spec_path: Path`.

**Outputs:** `ReadinessResult` (four `CheckResult` fields + `ready: bool`).

**External dependencies:** `git` CLI, `gh` CLI (authenticated).

---

## `file_watcher.py`

**Purpose:** Syncs filesystem task directories into `StateStore`. Called once at startup and optionally on each poll. Idempotent.

**`scan_pending(project)`:**
- Reads all `.md` files in `tasks/pending/`.
- Calls `state_store.add_task()` for each; skips duplicates silently.
- Returns count of newly inserted tasks.

**`scan_active(project)`:**
- Reads all `.md` files in `tasks/active/`.
- For files not already in DB: inserts as PENDING then immediately transitions to EXPLORE (legacy adoption path).
- Returns count of adopted tasks.

**`scan(project)`:** Calls both, returns total count.

**Error paths:**
- Directory absent → returns 0 (no exception).

**Obligatory inputs:** `state_store: StateStore` (constructor); `project: ProjectConfig` (per method).

**Outputs:** `int` (count of tasks inserted/adopted).

---

## `git_manager.py`

**Purpose:** All git subprocess operations. Pure executor — no state, no decisions.

**All functions** raise `subprocess.CalledProcessError` on failure (after logging stderr). Exception propagates to the scheduler.

**Functions:**

| Function | Description |
|---|---|
| `create_branch(repo_path, branch_name)` | `git checkout -b <branch>` |
| `checkout_branch(repo_path, branch_name)` | `git checkout <branch>` |
| `current_branch(repo_path)` | Returns current branch name string |
| `delete_branch(repo_path, branch_name, remote=False)` | `git branch -D`; optionally `git push origin --delete` (non-fatal) |
| `push_branch(repo_path, branch_name)` | `git push -u origin <branch>`; failure is non-fatal (logged as warning) |
| `commit_wip(repo_path, slug, retry_n)` | `git add -A && git commit "WIP: {slug} stuck attempt {n}"`. No-op if tree is clean. |
| `append_devlog(repo_path, slug, outcome, note)` | Appends one timestamped line to `devlog.md`. Creates file if absent. |
| `get_main_devlog(repo_path)` | Returns content of `main:devlog.md` via `git show`. Returns `""` if not found. |
| `get_retry_count(repo_path, slug)` | Counts `OUTCOME:stuck` entries for a slug in main's devlog. |
| `promote_devlog_to_main(repo_path, slug, branch_name)` | Diffs `devlog.md` between branch and main; appends new lines to main's devlog with a commit. No-op if no diff. |

**Error paths:**
- `push_branch` failure → warning log, no exception raised.
- `delete_branch` with `remote=True` → remote delete is `check=False` (non-fatal).
- All other operations → `CalledProcessError` propagates.

**Obligatory inputs:** `repo_path: Path` for all functions, plus function-specific arguments.

**Outputs:** `str` for `current_branch`, `get_main_devlog`; `int` for `get_retry_count`; `None` for all mutation functions.

---

## `telegram_bot.py`

**Purpose:** Telegram integration. Provides notification helpers and a background dispatcher thread that lets the operator query and control the orchestrator via chat commands.

### Low-level primitives

**`send_message(token, chat_id, text)`:** POST to Telegram API. Raises on non-2xx.

**`get_updates(token, offset, timeout)`:** Long-poll for updates. `timeout` default 30s.

### Notification helpers

**`notify_stuck(token, chat_id, task_slug, retry_n, branch_name)`:** Sends escalation alert with tmux attach instructions.

**`notify_uat_ready(token, chat_id, project_name, staging_url, changelog)`:** Sends UAT-ready notification.

**`make_notify_stuck(token, chat_id)`:** Returns a `Callable[[str, int, str], None]` bound to token/chat_id, suitable for `Scheduler.notify_stuck`.

### `TelegramCommandDispatcher` (background daemon thread)

**Main path:**
1. Polls `getUpdates` in a loop (long-poll, 10s timeout).
2. Filters messages to configured `chat_id` only.
3. Routes `/command` text to handler. Non-command messages are silently ignored.
4. On unhandled exception: logs, sleeps 5s, retries.

**Commands:**

| Command | Args | Effect |
|---|---|---|
| `/status` | — | Task counts by state |
| `/list` | `[project]` | Lists up to 20 tasks (optionally filtered by project) |
| `/start` | `<project> <slug>` | Informational — confirms task will dispatch on next poll (does not force immediate dispatch) |
| `/cancel` | `<slug>` | Calls `cancel_task` across all projects with that slug |
| `/approve` | — | Sets all pending approvals to approved |
| `/reject` | `[reason]` | Sets all pending approvals to rejected |
| `/hint` | `<slug> <text>` | Appends `## Human hint` block to task's `explore_guide` in DB |
| `/session` | `<slug>` | Spawns named tmux session at project's repo path; sends attach command |
| `/kill` | — | Sends SIGINT to orchestrator process |

**Approval coordination (used by scheduler for plan-approval flow):**
- `request_approval(slug, plan_text)` — registers pending approval, sends plan + instructions.
- `poll_approval(slug)` → `ApprovalResult | None` (None = still waiting).
- `clear_approval(slug)` — removes entry after resolution.

**`spawn_handoff_session(repo_path, task_slug, shell="fish")`:**
- Creates tmux session `autoclaude-{slug}` at `repo_path`.
- Raises `CalledProcessError` if tmux fails.

**Error paths:**
- `send_message` raises on non-2xx HTTP → propagates to caller.
- `get_updates` raises → dispatcher catches, logs, sleeps 5s.
- Command handlers catch all exceptions → reply "Error handling command — check logs."
- `/session` tmux failure → replies with error message, does not raise.

**Obligatory constructor inputs:** `token`, `chat_id`, `state_store`, `registry_path`.

**Optional constructor inputs:** `handoff_shell` (default `"fish"`).
