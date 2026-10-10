"""Current GitHub-state checks for discovered and recovered work items."""

import re
from collections.abc import Awaitable, Callable

from curupira.clients.gh import GhClient
from curupira.models import GhIssue, GhPullRequest, GhTaskViewRequest, Task

TaskValidator = Callable[[Task], Awaitable[Task | None]]
_CLOSING_REFERENCE = re.compile(
    r"\b(?:clos(?:e|es|ed)|fix(?:es|ed)?|resolv(?:e|es|ed))\s+"
    r"(?:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+))?#(?P<number>\d+)\b",
    flags=re.IGNORECASE,
)


class GitHubTaskRevalidator:
    """Refresh GitHub work before dispatch and compare completed PRs with linked issues."""

    def __init__(self, gh: GhClient) -> None:
        self._gh = gh

    async def __call__(self, task: Task) -> Task | None:
        """Return a refreshed open task, or ``None`` when it left its workflow queue."""
        if task.identity.task_type == "issue":
            return await self._revalidate_issue(task)
        if task.identity.task_type == "github-cli-pull-requests":
            return await self._revalidate_pull_request(task)
        return task

    async def verify_pull_request_completion(self, task: Task) -> bool:
        """Verify an apparently completed PR against its current linked issue state."""
        return await self.completion_next_action(task) is None

    async def completion_next_action(self, task: Task) -> str | None:
        """Return the explicit external follow-up when source work is still incomplete."""
        current = await self(task)
        if current is None:
            return None
        if task.identity.task_type == "issue":
            return "Create one draft pull request with a closing reference to this issue."
        if task.identity.task_type == "github-cli-pull-requests":
            if not current.linked_issue_numbers:
                return (
                    "Add a closing reference to the linked issue, then complete review and merge."
                )
            if all(state == "CLOSED" for state in current.linked_issue_states):
                return "The linked issue is closed while this PR is open; finish review and merge."
            return (
                "Recheck linked issue closure, remote checks, and review/merge status on this PR."
            )
        return None

    async def _revalidate_issue(self, task: Task) -> Task | None:
        issue = await self._gh.view_issue(_view_request(task))
        if _state(issue.state) != "OPEN":
            return None
        pulls = await self._gh.list_pull_requests_for_issue(
            task.identity.repo, int(task.identity.id)
        )
        if any(_closes_issue(pull, task.identity.repo, int(task.identity.id)) for pull in pulls):
            return None
        return task.model_copy(update=_issue_task_fields(issue))

    async def _revalidate_pull_request(self, task: Task) -> Task | None:
        pull = await self._gh.view_pull_request(_view_request(task))
        if _state(pull.state) != "OPEN" or pull.merged_at is not None:
            return None
        linked_numbers: list[str] = []
        linked_states: list[str] = []
        for reference in pull.closing_issues_references:
            linked_repo = reference.repository.name_with_owner if reference.repository else None
            if linked_repo is not None and linked_repo.casefold() != task.identity.repo.casefold():
                continue
            issue = await self._gh.view_issue(
                GhTaskViewRequest(repo=task.identity.repo, number=reference.number)
            )
            linked_numbers.append(str(reference.number))
            linked_states.append(_state(issue.state))
        return task.model_copy(
            update={
                "title": pull.title,
                "body": pull.body,
                "url": pull.url,
                "is_draft": pull.is_draft,
                "head_ref_name": pull.head_ref_name,
                "base_ref_name": pull.base_ref_name,
                "head_sha": pull.head_ref_oid,
                "mergeable": pull.mergeable,
                "merge_state_status": pull.merge_state_status,
                "check_conclusions": tuple(
                    check.conclusion or check.state or "UNKNOWN"
                    for check in pull.status_check_rollup
                ),
                "linked_issue_numbers": tuple(linked_numbers),
                "linked_issue_states": tuple(linked_states),
            }
        )


def _view_request(task: Task) -> GhTaskViewRequest:
    return GhTaskViewRequest(repo=task.identity.repo, number=int(task.identity.id))


def _issue_task_fields(issue: GhIssue) -> dict[str, str | None]:
    return {"title": issue.title, "body": issue.body, "url": issue.url}


def _state(value: str | None) -> str:
    return value.upper() if value is not None else "UNKNOWN"


def _closes_issue(pull: GhPullRequest, repo: str, issue_number: int) -> bool:
    for reference in pull.closing_issues_references:
        linked_repo = reference.repository.name_with_owner if reference.repository else None
        if reference.number == issue_number and (
            linked_repo is None or linked_repo.casefold() == repo.casefold()
        ):
            return True
    for match in _CLOSING_REFERENCE.finditer(pull.body or ""):
        linked_repo = match.group("repo")
        if int(match.group("number")) == issue_number and (
            linked_repo is None or linked_repo.casefold() == repo.casefold()
        ):
            return True
    return False
