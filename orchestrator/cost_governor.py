"""Cost logging to costs.jsonl. One JSON line per completed session."""

import json
from datetime import datetime, timezone
from pathlib import Path


def build_cost_record(
    project: str,
    task: str,
    model: str,
    outcome: str,
    turns: int,
    cost_usd: float,
) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "project": project,
        "task": task,
        "model": model,
        "outcome": outcome,
        "turns": turns,
        "cost_usd": cost_usd,
    }


def log_cost(costs_path: Path, record: dict) -> None:
    """Append one JSON line to costs.jsonl. Creates the file if absent."""
    with costs_path.open("a") as f:
        f.write(json.dumps(record) + "\n")
