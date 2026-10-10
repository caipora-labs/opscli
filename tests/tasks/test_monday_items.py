"""monday.com task discovery through mocked mcli subprocess results."""

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from typing_extensions import override

from curupira.clients.az import AzClient
from curupira.clients.gh import GhClient
from curupira.clients.process import AsyncProcessRunner
from curupira.errors import (
    CliExecutionError,
    CliLaunchError,
    CliNotFoundError,
    CliTimeoutError,
    DispatchError,
)
from curupira.models import CommandRequest, PollingSettings, ProcessResult
from curupira.storage import CronScheduleRepository
from curupira.tasks.base import FeedDependencies
from curupira.tasks.feed import PollingTaskFeed
from curupira.tasks.monday_items import MondayItemSource, MondayItemTrigger
from curupira.tasks.registry import get
from tests.helpers import resolved_automation


class RecordingRunner(AsyncProcessRunner):
    """Return queued JSON pages and record mcli argument vectors."""

    def __init__(self, *results: ProcessResult) -> None:
        super().__init__()
        self.results = list(results)
        self.requests: list[CommandRequest] = []

    @override
    async def run(
        self,
        request: CommandRequest,
        *,
        on_stdout_line: Callable[[str], Awaitable[None]] | None = None,
    ) -> ProcessResult:
        assert on_stdout_line is None
        self.requests.append(request)
        return self.results.pop(0)


class MissingMcliRunner(AsyncProcessRunner):
    """Simulate a machine without the mcli executable."""

    @override
    async def run(
        self,
        request: CommandRequest,
        *,
        on_stdout_line: Callable[[str], Awaitable[None]] | None = None,
    ) -> ProcessResult:
        del request, on_stdout_line
        raise CliNotFoundError("mcli")


class FailedMcliRunner(AsyncProcessRunner):
    """Raise a controlled process error to exercise launch/execution failures."""

    def __init__(self, error: DispatchError) -> None:
        super().__init__()
        self.error = error

    @override
    async def run(
        self,
        request: CommandRequest,
        *,
        on_stdout_line: Callable[[str], Awaitable[None]] | None = None,
    ) -> ProcessResult:
        del request, on_stdout_line
        raise self.error


@pytest.mark.asyncio
async def test_source_reads_json_pages_and_preserves_ids_as_strings(tmp_path: Path) -> None:
    runner = RecordingRunner(
        ProcessResult(
            returncode=0,
            stdout=(
                '{"items":[{"id":"9007199254740993","name":"First","state":"active",'
                '"group":{"id":"topics","title":"Sprint 1"},"columns":[]},'
                '{"id":"2","name":"Second","state":"active"}],"cursor":"next"}'
            ),
        ),
        ProcessResult(
            returncode=0,
            stdout='{"items":[{"id":"2","name":"Duplicate"},{"id":"3","name":"Third"}],"cursor":""}',
        ),
    )
    automation = resolved_automation(tmp_path, "monday-work", "monday-cli-items")

    tasks = await MondayItemSource(runner).discover(automation, 3)

    assert [task.identity.id for task in tasks] == ["9007199254740993", "2", "3"]
    assert tasks[0].title == "First"
    assert tasks[0].url.endswith("/boards/12345678901234567/pulses/9007199254740993")
    assert runner.requests[0].arguments == (
        "item",
        "list",
        "--board",
        "12345678901234567",
        "--limit",
        "3",
        "--json",
    )
    assert runner.requests[1].arguments[-2:] == ("--cursor", "next")


@pytest.mark.asyncio
async def test_source_returns_empty_for_empty_board(tmp_path: Path) -> None:
    runner = RecordingRunner(ProcessResult(returncode=0, stdout='{"items":[],"cursor":""}'))

    tasks = await MondayItemSource(runner).discover(
        resolved_automation(tmp_path, "monday-work", "monday-cli-items"), 10
    )

    assert tasks == []


@pytest.mark.asyncio
async def test_source_pages_are_deduplicated_between_repeated_polls(tmp_path: Path) -> None:
    runner = RecordingRunner(
        ProcessResult(returncode=0, stdout='{"items":[{"id":"1","name":"Task"}],"cursor":""}'),
        ProcessResult(returncode=0, stdout='{"items":[{"id":"1","name":"Task"}],"cursor":""}'),
    )
    automation = resolved_automation(tmp_path, "monday-work", "monday-cli-items")
    feed = PollingTaskFeed(automation, PollingSettings(), MondayItemSource(runner))

    first_poll = await feed.poll()
    second_poll = await feed.poll()

    assert [task.identity.id for task in first_poll] == ["1"]
    assert second_poll == []


@pytest.mark.asyncio
async def test_source_resumes_next_page_on_later_poll(tmp_path: Path) -> None:
    runner = RecordingRunner(
        ProcessResult(
            returncode=0,
            stdout='{"items":[{"id":"1","name":"One"},{"id":"2","name":"Two"}],"cursor":"next"}',
        ),
        ProcessResult(
            returncode=0,
            stdout='{"items":[{"id":"3","name":"Three"},{"id":"4","name":"Four"}],"cursor":""}',
        ),
    )
    automation = resolved_automation(tmp_path, "monday-work", "monday-cli-items")
    feed = PollingTaskFeed(automation, PollingSettings(batch_size=2), MondayItemSource(runner))

    first_poll = await feed.poll()
    second_poll = await feed.poll()

    assert [task.identity.id for task in first_poll] == ["1", "2"]
    assert [task.identity.id for task in second_poll] == ["3", "4"]
    assert runner.requests[1].arguments[-2:] == ("--cursor", "next")


@pytest.mark.asyncio
async def test_source_reports_missing_cli_with_install_help(tmp_path: Path) -> None:
    source = MondayItemSource(MissingMcliRunner())

    with pytest.raises(DispatchError, match=r"mcli is not installed.*go install"):
        await source.discover(resolved_automation(tmp_path, "monday-work", "monday-cli-items"), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "message"),
    [
        (ProcessResult(returncode=4, stderr="authentication token missing"), "mcli auth status"),
        (ProcessResult(returncode=0, stdout="not json"), "unexpected JSON"),
    ],
)
async def test_source_reports_cli_and_json_errors(
    tmp_path: Path, result: ProcessResult, message: str
) -> None:
    source = MondayItemSource(RecordingRunner(result))

    with pytest.raises(DispatchError, match=message):
        await source.discover(resolved_automation(tmp_path, "monday-work", "monday-cli-items"), 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "message"),
    [
        (CliLaunchError("mcli", "permission denied"), "could not start mcli"),
        (CliExecutionError("mcli", 3, "invalid authentication token"), "mcli auth status"),
        (CliTimeoutError("mcli", 60.0), "mcli item list timed out"),
    ],
)
async def test_source_reports_process_failures(
    tmp_path: Path, error: DispatchError, message: str
) -> None:
    source = MondayItemSource(FailedMcliRunner(error))

    with pytest.raises(DispatchError, match=message):
        await source.discover(resolved_automation(tmp_path, "monday-work", "monday-cli-items"), 1)


def test_trigger_is_registered_and_builds_polling_feed(tmp_path: Path) -> None:
    automation = resolved_automation(tmp_path, "monday-work", "monday-cli-items")
    dependencies = FeedDependencies(
        polling=PollingSettings(),
        gh=GhClient(),
        az=AzClient(),
        cron=CronScheduleRepository(tmp_path / "state.sqlite3"),
        state_db_path=tmp_path / "state.sqlite3",
        runner=RecordingRunner(),
    )

    trigger = get("monday-cli-items")
    feed = trigger.build_feed(automation, dependencies)

    assert isinstance(trigger, MondayItemTrigger)
    assert isinstance(feed, PollingTaskFeed)
    assert trigger.prompt_fields() == frozenset(
        {"monday_item_id", "monday_item_title", "monday_board_id"}
    )
