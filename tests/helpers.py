"""Reusable typed task snapshots and controlled CLI adapters for tests."""

from pathlib import Path

from curupira.config import ApplicationSettings
from curupira.models import ResolvedAutomation, Task, TaskIdentity


def resolved_automation(
    path: Path, name: str = "issues", trigger: str = "issue", **overrides: object
) -> ResolvedAutomation:
    """Resolve one automation through the real configuration boundary."""
    repo = "contoso/api-project/api" if trigger == "azure-cli-pull-requests" else "acme/api"
    config: dict[str, object] = {
        "trigger_type": trigger,
        "repo": repo,
        "path": path,
        "prompt": "Handle ${task_type} ${task_number}: ${task_title}",
    }
    if trigger == "cron":
        config.update(schedule="0 9 * * *", start_date="2026-10-01T00:00:00+00:00")
    elif trigger == "monday-cli-items":
        config["board_id"] = "12345678901234567"
    elif trigger == "trello-cli-cards":
        config["board_id"] = "board123"
    elif trigger != "azure-cli-pull-requests":
        config["query"] = "is:open"
    config.update(overrides)
    settings = ApplicationSettings.model_validate(
        {"coding_agents": {"automations": {name: config}}}
    )
    return settings.resolve_automations()[name]


def issue_task(path: Path, number: int = 42, name: str = "issues") -> Task:
    """Create an issue task with a fully resolved, validated execution snapshot."""
    automation = resolved_automation(path, name)
    return Task(
        identity=TaskIdentity(
            automation_id=name, repo="acme/api", task_type="issue", id=str(number)
        ),
        automation=automation,
        title=f"Task {number}",
        body="Details",
        url=f"https://github.com/acme/api/issues/{number}",
    )


def pull_request_task(path: Path, number: int = 12, name: str = "reviews") -> Task:
    """Create a pull request snapshot with its native branch metadata."""
    return Task(
        identity=TaskIdentity(
            automation_id=name,
            repo="acme/api",
            task_type="github-cli-pull-requests",
            id=str(number),
        ),
        automation=resolved_automation(path, name, "github-cli-pull-requests"),
        title="Review",
        url=f"https://github.com/acme/api/pull/{number}",
        is_draft=True,
        head_ref_name="feature",
        base_ref_name="main",
    )
