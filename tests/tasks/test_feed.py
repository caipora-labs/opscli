"""Generic task-source polling behavior."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from typing_extensions import override

from curupira.errors import DispatchError
from curupira.models import PollingSettings, ResolvedAutomation, Task
from curupira.tasks.base import TaskFeed, TaskSource
from curupira.tasks.feed import PollingTaskFeed, merge_task_streams, poll_task_feeds
from tests.helpers import issue_task, pull_request_task, resolved_automation


class FakeTaskSource(TaskSource):
    """Return recorded batches without a remote dependency."""

    def __init__(self, responses: list[list[Task]]) -> None:
        self.responses = responses
        self.calls: list[tuple[ResolvedAutomation, int]] = []

    @override
    async def discover(self, automation: ResolvedAutomation, limit: int) -> list[Task]:
        self.calls.append((automation, limit))
        return self.responses.pop(0) if self.responses else []


class StopPollingError(Exception):
    """Stop a deterministic polling test after its expected waits."""


async def test_polling_deduplicates_and_preview_does_not_mark_seen(tmp_path: Path) -> None:
    task = issue_task(tmp_path)
    automation = resolved_automation(tmp_path)
    source = FakeTaskSource([[task], [task], [task]])
    feed = PollingTaskFeed(automation, PollingSettings(batch_size=8), source)

    assert await feed.poll(preview=True) == [task]
    assert await feed.poll() == [task]
    assert await feed.poll() == []
    assert source.calls[0] == (automation, 8)


async def test_polling_re_admits_pull_request_after_head_changes(tmp_path: Path) -> None:
    first = pull_request_task(tmp_path).model_copy(update={"head_sha": "head-1"})
    changed = first.model_copy(update={"head_sha": "head-2"})
    source = FakeTaskSource([[first], [first], [changed]])
    feed = PollingTaskFeed(
        resolved_automation(tmp_path, "reviews", "github-cli-pull-requests"),
        PollingSettings(),
        source,
    )

    assert await feed.poll() == [first]
    assert await feed.poll() == []
    assert await feed.poll() == [changed]


async def test_feed_poll_races_do_not_override_workflow_stage_order(tmp_path: Path) -> None:
    class BatchFeed(TaskFeed):
        def __init__(self, tasks: list[Task], delay: float) -> None:
            self.tasks = tasks
            self.delay = delay

        @override
        async def poll(self, *, preview: bool = False) -> list[Task]:
            del preview
            await asyncio.sleep(self.delay)
            return self.tasks

        @override
        async def stream(self) -> AsyncIterator[Task]:
            for task in self.tasks:
                yield task

    issue = issue_task(tmp_path)
    ready_pull = pull_request_task(tmp_path, 12).model_copy(
        update={
            "is_draft": False,
            "head_sha": "head-12",
            "mergeable": "MERGEABLE",
            "merge_state_status": "CLEAN",
            "check_conclusions": ("SUCCESS",),
        }
    )
    available = await poll_task_feeds([BatchFeed([issue], 0), BatchFeed([ready_pull], 0.01)])

    assert available == [ready_pull, issue]


async def test_backoff_resets_after_discovery(tmp_path: Path) -> None:
    waits: list[float] = []

    async def sleep(delay: float) -> None:
        waits.append(delay)
        if len(waits) == 4:
            raise StopPollingError

    task = issue_task(tmp_path)
    source = FakeTaskSource([[], [], [task], []])
    feed = PollingTaskFeed(
        resolved_automation(tmp_path),
        PollingSettings(poll_interval_seconds=17),
        source,
        sleep=sleep,
    )
    stream = feed.stream()
    assert (await anext(stream)).identity == task.identity
    with pytest.raises(StopPollingError):
        await anext(stream)
    assert waits == [17, 34, 17, 34]


async def test_backoff_is_bounded_and_retries_dispatch_errors(tmp_path: Path) -> None:
    class FailingSource(TaskSource):
        @override
        async def discover(self, automation: ResolvedAutomation, limit: int) -> list[Task]:
            raise DispatchError("temporary failure")

    waits: list[float] = []

    async def sleep(delay: float) -> None:
        waits.append(delay)
        if len(waits) == 3:
            raise StopPollingError

    feed = PollingTaskFeed(
        resolved_automation(tmp_path),
        PollingSettings(poll_interval_seconds=200),
        FailingSource(),
        sleep=sleep,
    )
    with pytest.raises(StopPollingError):
        await anext(feed.stream())
    assert waits == [200, 300, 300]


async def test_merge_closes_producers_when_cancelled(tmp_path: Path) -> None:
    closed = asyncio.Event()

    async def infinite() -> AsyncIterator[Task]:
        try:
            while True:
                yield issue_task(tmp_path)
        finally:
            closed.set()

    merged = merge_task_streams([infinite()], max_pending=1)
    await anext(merged)
    await asyncio.sleep(0)
    await asyncio.wait_for(merged.aclose(), timeout=1)
    assert closed.is_set()


async def test_merge_drains_finite_sources_and_propagates_errors(tmp_path: Path) -> None:
    async def finite(number: int) -> AsyncIterator[Task]:
        yield issue_task(tmp_path, number)

    async def broken() -> AsyncIterator[Task]:
        raise DispatchError("broken source")
        yield issue_task(tmp_path)

    merged = [task async for task in merge_task_streams([finite(1), finite(2)], max_pending=1)]
    assert {task.identity.id for task in merged} == {"1", "2"}
    with pytest.raises(DispatchError, match="broken source"):
        async for _ in merge_task_streams([finite(3), broken()], max_pending=1):
            pass
