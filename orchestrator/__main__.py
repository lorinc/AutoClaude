"""
Entry point. Loads .env, validates environment, configures logging, starts scheduler.

Fail-fast conditions (checked before scheduler starts):
- project_registry.json missing (gitignored — must be created manually on each machine)
- ANTHROPIC_API_KEY not set (claude CLI will fail without it)
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger

REPO_ROOT = Path(__file__).parent.parent


def _load_env() -> None:
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        load_dotenv(env_file)


def _configure_logging() -> None:
    """Set up loguru: stderr (INFO) + rotating file (DEBUG)."""
    logger.remove()  # remove default handler
    logger.add(sys.stderr, level="INFO",
               format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> <level>{level:<8}</level> {message}")
    log_file = REPO_ROOT / "orchestrator.log"
    logger.add(str(log_file), rotation="10 MB", retention=5, level="DEBUG",
               format="{time:YYYY-MM-DD HH:mm:ss} {level:<8} {message}")
    logger.debug("Logging configured. Log file: {}", log_file)


def _check_prerequisites(registry_path: Path) -> None:
    if not registry_path.exists():
        example = REPO_ROOT / "project_registry.example.json"
        logger.error(
            "project_registry.json not found at {}\n"
            "This file is gitignored — create it on this machine.\n"
            "See {} for the required format.",
            registry_path, example,
        )
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error(
            "ANTHROPIC_API_KEY is not set. Add it to {} or export it in the shell.",
            REPO_ROOT / ".env",
        )
        sys.exit(1)


def _log_startup(registry_path: Path, dry_run: bool, env: str) -> None:
    from orchestrator.scheduler import load_registry
    projects = load_registry(registry_path)
    logger.info("AutoClaude starting. env={} dry_run={}", env, dry_run)
    logger.info("Registered projects: {}", len(projects))
    for p in projects:
        pending_dir = p.repo_path / "tasks" / "pending"
        active_dir = p.repo_path / "tasks" / "active"
        pending = list(pending_dir.glob("*.md")) if pending_dir.exists() else []
        active = list(active_dir.glob("*.md")) if active_dir.exists() else []
        logger.info(
            "  [{}] pending={} active(legacy)={} budget={:.2f}",
            p.name, len(pending), len(active), p.budget_limit_usd,
        )


def main() -> None:
    _load_env()
    _configure_logging()

    registry_path = REPO_ROOT / "project_registry.json"
    _check_prerequisites(registry_path)

    env = os.environ.get("ORCHESTRATOR_ENV", "dev")
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    poll_interval = int(os.environ.get("POLL_INTERVAL", "60"))
    turn_limit = int(os.environ.get("TURN_LIMIT", "15"))
    max_retries = int(os.environ.get("MAX_RETRIES", "3"))
    handoff_shell = os.environ.get("HANDOFF_SHELL", "fish")

    init_prompt_path = REPO_ROOT / "init_prompt.md"
    init_prompt = init_prompt_path.read_text() if init_prompt_path.exists() else ""

    _log_startup(registry_path, dry_run, env)

    # SQLite state store
    from orchestrator.state_store import StateStore
    db_path = REPO_ROOT / "tasks.db"
    state_store = StateStore(db_path)
    logger.info("State store: {}", db_path)

    # Startup scan: adopt any tasks already in tasks/pending/ or tasks/active/
    from orchestrator.file_watcher import FileWatcher
    from orchestrator.scheduler import load_registry
    watcher = FileWatcher(state_store)
    for project in load_registry(registry_path):
        n = watcher.scan(project)
        if n:
            logger.info("[{}] Startup scan: {} task(s) adopted", project.name, n)

    # Telegram
    notify_stuck = None
    tg_dispatcher = None
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat_id:
        from orchestrator.telegram_bot import TelegramCommandDispatcher, make_notify_stuck
        notify_stuck = make_notify_stuck(tg_token, tg_chat_id)
        tg_dispatcher = TelegramCommandDispatcher(
            token=tg_token,
            chat_id=tg_chat_id,
            state_store=state_store,
            registry_path=registry_path,
            handoff_shell=handoff_shell,
        )
        tg_dispatcher.daemon = True
        tg_dispatcher.start()
        logger.info("Telegram dispatcher started (chat_id={})", tg_chat_id)
    else:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — Telegram disabled")

    from orchestrator.scheduler import Scheduler
    scheduler = Scheduler(
        registry_path=registry_path,
        init_prompt=init_prompt,
        turn_limit=turn_limit,
        max_retries=max_retries,
        poll_interval=poll_interval,
        dry_run=dry_run,
        state_store=state_store,
        notify_stuck=notify_stuck,
        costs_path=REPO_ROOT / "costs.jsonl",
    )
    try:
        scheduler.run()
    finally:
        if tg_dispatcher is not None:
            tg_dispatcher.stop()
        state_store.close()
        logger.info("AutoClaude shut down.")


if __name__ == "__main__":
    main()
