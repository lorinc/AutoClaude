"""Definition of Ready check. Runs before any agent session starts.

Returns a ReadinessResult with pass/fail per check and an aggregate ready flag.
The orchestrator acts on the result — this module only reports, never decides.
"""

import json
import subprocess
from pathlib import Path

from pydantic import BaseModel, model_validator

REQUIRED_SECTIONS = frozenset({
    "## Goal",
    "## Context and constraints",
    "## My concerns",
    "## Acceptance criteria",
})

AGENT_BRANCH_PREFIXES = ("feature/", "fix/", "chore/")

MODEL_MAP: dict[str, str] = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
}

_DESIGN_KEYWORDS = ("architect", "design")
_CHORE_KEYWORDS = ("chore", "cleanup", "refactor", "deps", "lint", "format", "bump")


class CheckResult(BaseModel):
    passed: bool
    reason: str | None = None


class ReadinessResult(BaseModel):
    task_spec: CheckResult
    branch_clean: CheckResult
    no_duplicate_pr: CheckResult
    ci_green: CheckResult
    ready: bool = False

    @model_validator(mode="after")
    def compute_ready(self) -> "ReadinessResult":
        self.ready = all([
            self.task_spec.passed,
            self.branch_clean.passed,
            self.no_duplicate_pr.passed,
            self.ci_green.passed,
        ])
        return self


def check_task_spec(spec_path: Path) -> CheckResult:
    """Validate that the task spec contains all four required section headers."""
    try:
        content = spec_path.read_text()
    except FileNotFoundError:
        return CheckResult(passed=False, reason=f"Task spec not found: {spec_path}")

    present = {line.strip() for line in content.splitlines()}
    missing = sorted(REQUIRED_SECTIONS - present)
    if missing:
        return CheckResult(passed=False, reason=f"Missing sections: {missing}")
    return CheckResult(passed=True)


def check_branch_clean(repo_path: Path) -> CheckResult:
    """Check no uncommitted changes and no unmerged agent branches on the project repo."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_path, capture_output=True, text=True,
    )
    if status.returncode != 0:
        return CheckResult(passed=False, reason=f"git status failed: {status.stderr.strip()}")
    if status.stdout.strip():
        return CheckResult(passed=False, reason="Uncommitted changes present")

    unmerged_result = subprocess.run(
        ["git", "branch", "--no-merged", "main"],
        cwd=repo_path, capture_output=True, text=True,
    )
    if unmerged_result.returncode != 0:
        return CheckResult(passed=False, reason=f"git branch failed: {unmerged_result.stderr.strip()}")

    unmerged = [
        b.strip().removeprefix("* ")
        for b in unmerged_result.stdout.splitlines()
        if any(b.strip().removeprefix("* ").startswith(p) for p in AGENT_BRANCH_PREFIXES)
    ]
    if unmerged:
        return CheckResult(passed=False, reason=f"Unmerged agent branches: {unmerged}")
    return CheckResult(passed=True)


def check_no_duplicate_pr(github_url: str, task_slug: str) -> CheckResult:
    """Check no open PR already exists for this task slug (any branch prefix)."""
    for prefix in AGENT_BRANCH_PREFIXES:
        branch = f"{prefix}{task_slug}"
        result = subprocess.run(
            ["gh", "pr", "list", "--repo", github_url, "--head", branch,
             "--state", "open", "--json", "number"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return CheckResult(passed=False, reason=f"gh pr list failed: {result.stderr.strip()}")
        prs = json.loads(result.stdout)
        if prs:
            return CheckResult(
                passed=False,
                reason=f"Open PR already exists for {branch}: #{prs[0]['number']}",
            )
    return CheckResult(passed=True)


def check_ci_green(github_url: str) -> CheckResult:
    """Check that the last completed GH Actions run on main succeeded."""
    result = subprocess.run(
        ["gh", "run", "list", "--repo", github_url,
         "--branch", "main", "--status", "completed",
         "--limit", "1", "--json", "conclusion"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return CheckResult(passed=False, reason=f"gh run list failed: {result.stderr.strip()}")
    try:
        runs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return CheckResult(passed=False, reason="Failed to parse gh run list output")
    if not runs:
        return CheckResult(passed=True, reason="No CI runs on main yet")
    conclusion = runs[0].get("conclusion", "")
    if conclusion != "success":
        return CheckResult(passed=False, reason=f"Last CI run on main: {conclusion}")
    return CheckResult(passed=True)


def _infer_model_from_slug(slug: str) -> str:
    if any(kw in slug for kw in _DESIGN_KEYWORDS):
        return MODEL_MAP["opus"]
    if any(kw in slug for kw in _CHORE_KEYWORDS):
        return MODEL_MAP["haiku"]
    return MODEL_MAP["sonnet"]


def parse_task_model(spec_path: Path) -> str:
    """Return the Claude model ID for this task.

    Reads optional '## Model' section (haiku / sonnet / opus).
    Falls back to slug-based inference when the section is absent.
    """
    slug = spec_path.stem.lower()
    try:
        content = spec_path.read_text()
    except FileNotFoundError:
        return _infer_model_from_slug(slug)

    lines = content.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "## Model":
            for value in lines[i + 1:]:
                value = value.strip().lower()
                if value:
                    return MODEL_MAP.get(value, MODEL_MAP["sonnet"])
            break

    return _infer_model_from_slug(slug)


def run(spec_path: Path, repo_path: Path, github_url: str, task_slug: str) -> ReadinessResult:
    return ReadinessResult(
        task_spec=check_task_spec(spec_path),
        branch_clean=check_branch_clean(repo_path),
        no_duplicate_pr=check_no_duplicate_pr(github_url, task_slug),
        ci_green=check_ci_green(github_url),
    )
