"""
Entry point. Loads .env, validates environment, logs startup state, starts scheduler.

Fail-fast conditions (checked before scheduler starts):
- project_registry.json missing (gitignored — must be created manually on each machine)
- ANTHROPIC_API_KEY not set (claude CLI will fail without it)
"""

import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).parent.parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("autoclaude")


def _load_env() -> None:
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        load_dotenv(env_file)


def _check_prerequisites(registry_path: Path) -> None:
    if not registry_path.exists():
        example = REPO_ROOT / "project_registry.example.json"
        logger.error(
            "project_registry.json not found at %s\n"
            "This file is gitignored — create it on this machine.\n"
            "See %s for the required format.",
            registry_path, example,
        )
        sys.exit(1)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.error(
            "ANTHROPIC_API_KEY is not set. "
            "Add it to %s or export it in the shell.", REPO_ROOT / ".env",
        )
        sys.exit(1)


def _log_startup(registry_path: Path, dry_run: bool, env: str) -> None:
    from orchestrator.scheduler import load_registry
    projects = load_registry(registry_path)
    logger.info("AutoClaude starting. env=%s dry_run=%s", env, dry_run)
    logger.info("Registered projects: %d", len(projects))
    for p in projects:
        pending = list((p.repo_path / "tasks" / "pending").glob("*.md")) if (p.repo_path / "tasks" / "pending").exists() else []
        active = list((p.repo_path / "tasks" / "active").glob("*.md")) if (p.repo_path / "tasks" / "active").exists() else []
        logger.info(
            "  [%s] pending=%d active(retry)=%d budget=%.2f",
            p.name, len(pending), len(active), p.budget_limit_usd,
        )


def main() -> None:
    _load_env()

    registry_path = REPO_ROOT / "project_registry.json"
    _check_prerequisites(registry_path)

    env = os.environ.get("ORCHESTRATOR_ENV", "dev")
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    poll_interval = int(os.environ.get("POLL_INTERVAL", "60"))
    turn_limit = int(os.environ.get("TURN_LIMIT", "15"))
    max_retries = int(os.environ.get("MAX_RETRIES", "3"))

    init_prompt_path = REPO_ROOT / "init_prompt.md"
    init_prompt = init_prompt_path.read_text() if init_prompt_path.exists() else ""

    _log_startup(registry_path, dry_run, env)

    notify_stuck = None
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat_id:
        from orchestrator.telegram_bot import make_notify_stuck
        notify_stuck = make_notify_stuck(tg_token, tg_chat_id)
        logger.info("Telegram notifications enabled (chat_id=%s)", tg_chat_id)
    else:
        logger.warning("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — stuck alerts will log only")

    from orchestrator.scheduler import Scheduler
    scheduler = Scheduler(
        registry_path=registry_path,
        init_prompt=init_prompt,
        turn_limit=turn_limit,
        max_retries=max_retries,
        poll_interval=poll_interval,
        dry_run=dry_run,
        notify_stuck=notify_stuck,
    )
    scheduler.run()


if __name__ == "__main__":
    main()
