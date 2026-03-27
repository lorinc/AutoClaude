#!/usr/bin/env bash
# Claude Code Stop hook — appends a minimal devlog entry if the agent didn't write one.
#
# Installed per-project via .claude/settings.json:
#   {"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "/path/to/devlog-guard.sh"}]}]}}
#
# Requires env vars set by the orchestrator before launching the session:
#   AUTOCLAUDE_TASK_SLUG  — current task slug
#   AUTOCLAUDE_REPO_PATH  — absolute path to the project repo

set -euo pipefail

SLUG="${AUTOCLAUDE_TASK_SLUG:-}"
REPO="${AUTOCLAUDE_REPO_PATH:-}"

# Not running under orchestrator — no-op
[[ -z "$SLUG" || -z "$REPO" ]] && exit 0

DEVLOG="$REPO/devlog.md"
TIMESTAMP=$(date -u +"%Y-%m-%d %H:%M")

# If the agent wrote an entry for this task in the last 10 lines, nothing to do
if tail -10 "$DEVLOG" 2>/dev/null | grep -q "TASK:$SLUG"; then
    exit 0
fi

echo "[$TIMESTAMP] TASK:$SLUG OUTCOME:unknown NOTE:session ended — no devlog entry written by agent" >> "$DEVLOG"
