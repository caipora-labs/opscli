"""GitHub pull-request task source and trigger behavior."""

from pathlib import Path

import pytest
from typing_extensions import override

from curupira.clients.az import AzClient
from curupira.clients.gh import GhClient
from curupira.models import GhPullRequest, GhPullRequestSearchRequest, PollingSettings
from curupira.storage import CronScheduleRepository
from curupira.tasks.base import FeedDependencies
from curupira.tasks.feed import PollingTaskFeed
from curupira.tasks.github_pull_requests import GitHubPullRequestSource, PullRequestTrigger
from curupira.tasks.registry import get
from tests.helpers import pull_request_task, resolved_automation


class FakeGhClient(GhClient):
    """Return controlled pull requests and retain the search request."""

    def __init__(self, pull_requests: list[GhPullRequest]) -> None:
        super().__init__()
        self.pull_requests = pull_requests
        self.request: GhPullRequestSearchRequest | None = None

    @override
    async def list_pull_requests(self, request: GhPullRequestSearchRequest) -> list[GhPullRequest]:
        self.request = request
        return self.pull_requests


@pytest.mark.asyncio
async def test_source_searches_pull_requests_and_builds_tasks(tmp_path: Path) -> None:
    gh = FakeGhClient(
        [
            GhPullRequest(
                number=42,
                title="Review change",
                body="Details",
                url="https://github.com/acme/api/pull/42",
                isDraft=True,
                headRefName="feature",
                baseRefName="main",
                headRefOid="head-42",
                mergeable="MERGEABLE",
                mergeStateStatus="CLEAN",
                statusCheckRollup=[{"conclusion": "SUCCESS"}],
            )
        ]
    )
    automation = resolved_automation(
        tmp_path,
        "reviews",
        "github-cli-pull-requests",
        jq='.[] | select(.mergeable == "MERGEABLE")',
    )

    tasks = await GitHubPullRequestSource(gh).discover(automation, 7)

    assert gh.request == GhPullRequestSearchRequest(
        repo="acme/api",
        query="is:open",
        limit=7,
        jq='.[] | select(.mergeable == "MERGEABLE")',
    )
    assert len(tasks) == 1
    assert tasks[0].identity.id == "42"
    assert tasks[0].identity.task_type == "github-cli-pull-requests"
    assert tasks[0].title == "Review change"
    assert tasks[0].body == "Details"
    assert tasks[0].url == "https://github.com/acme/api/pull/42"
    assert tasks[0].is_draft is True
    assert tasks[0].head_ref_name == "feature"
    assert tasks[0].base_ref_name == "main"
    assert tasks[0].head_sha == "head-42"
    assert tasks[0].merge_state_status == "CLEAN"
    assert tasks[0].check_conclusions == ("SUCCESS",)


def test_pull_request_trigger_is_registered_and_provides_prompt_context(tmp_path: Path) -> None:
    trigger = get("github-cli-pull-requests")
    assert trigger.trigger_type == "github-cli-pull-requests"
    task = pull_request_task(tmp_path, number=54)

    assert isinstance(trigger, PullRequestTrigger)
    assert trigger.prompt_fields() == frozenset(
        {
            "pull_request_number",
            "pull_request_title",
            "pull_request_body",
            "pull_request_url",
            "pull_request_is_draft",
            "pull_request_head_ref",
            "pull_request_base_ref",
        }
    )
    assert trigger.prompt_context(task) == {
        "pull_request_number": "54",
        "pull_request_title": "Review",
        "pull_request_body": "",
        "pull_request_url": "https://github.com/acme/api/pull/54",
        "pull_request_is_draft": "true",
        "pull_request_head_ref": "feature",
        "pull_request_base_ref": "main",
    }


def test_pull_request_trigger_builds_polling_feed(tmp_path: Path) -> None:
    automation = resolved_automation(tmp_path, "reviews", "github-cli-pull-requests")
    gh = FakeGhClient([])

    feed = PullRequestTrigger().build_feed(
        automation,
        FeedDependencies(
            polling=PollingSettings(),
            gh=gh,
            az=AzClient(),
            cron=CronScheduleRepository(tmp_path / "state.sqlite3"),
            state_db_path=tmp_path / "state.sqlite3",
        ),
    )

    assert isinstance(feed, PollingTaskFeed)
    assert feed.automation is automation
