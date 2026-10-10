"""GitHub task validation against current fake source state."""

from pathlib import Path

from curupira.models import GhIssue, GhIssueReference, GhPullRequest, GhRepositoryReference
from curupira.tasks.revalidation import GitHubTaskRevalidator
from tests.fakes import FakeGitHub
from tests.helpers import issue_task, pull_request_task


async def test_issue_with_open_closing_pr_is_not_eligible(tmp_path: Path) -> None:
    task = issue_task(tmp_path)
    github = FakeGitHub(
        issues=[
            GhIssue(
                number=42,
                title="Current issue",
                url="https://github.com/acme/api/issues/42",
                state="OPEN",
            )
        ],
        pulls=[
            GhPullRequest(
                number=12,
                title="Implements issue",
                body="Fixes #42",
                url="https://github.com/acme/api/pull/12",
                state="OPEN",
            )
        ],
    )

    assert await GitHubTaskRevalidator(github)(task) is None


async def test_closed_issue_is_not_eligible_even_when_it_matches_old_snapshot(
    tmp_path: Path,
) -> None:
    task = issue_task(tmp_path)
    github = FakeGitHub(
        issues=[
            GhIssue(
                number=42,
                title=task.title,
                url=task.url,
                state="CLOSED",
            )
        ]
    )

    assert await GitHubTaskRevalidator(github)(task) is None


async def test_open_pull_request_completion_checks_linked_issue_state(tmp_path: Path) -> None:
    task = pull_request_task(tmp_path, 12)
    linked_issue = GhIssue(
        number=42,
        title="Issue",
        url="https://github.com/acme/api/issues/42",
        state="OPEN",
    )
    pull = GhPullRequest(
        number=12,
        title="Review",
        url="https://github.com/acme/api/pull/12",
        state="OPEN",
        closingIssuesReferences=[
            GhIssueReference(
                number=42,
                repository=GhRepositoryReference(nameWithOwner="acme/api"),
            )
        ],
    )
    github = FakeGitHub(issues=[linked_issue], pulls=[pull])
    revalidator = GitHubTaskRevalidator(github)

    assert await revalidator.completion_next_action(task) == (
        "Recheck linked issue closure, remote checks, and review/merge status on this PR."
    )

    github.issues = [linked_issue.model_copy(update={"state": "CLOSED"})]
    assert await revalidator.completion_next_action(task) == (
        "The linked issue is closed while this PR is open; finish review and merge."
    )
    assert not await revalidator.verify_pull_request_completion(task)
