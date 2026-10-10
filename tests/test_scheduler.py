"""Shared concurrency, checkout exclusivity, failure observation, and recovery."""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

from typing_extensions import override

from curupira.agents.base import SessionStartedCallback
from curupira.executor import TaskExecutor
from curupira.models import (
    CodingTaskRequest,
    ExecutionSettings,
    GhIssue,
    GhPullRequest,
    ProcessResult,
    RunningCodingSession,
    Task,
)
from curupira.scheduler import TaskScheduler
from curupira.storage import (
    CompletedTaskRepository,
    CronScheduleRepository,
    RunningSessionRepository,
)
from curupira.tasks.revalidation import GitHubTaskRevalidator
from tests.fakes import FakeGitHub, FakeVersionControl, RecordingAdapter
from tests.helpers import issue_task, pull_request_task


async def stream(tasks: list[Task]) -> AsyncIterator[Task]:
    """Yield a finite set of tasks with cooperative checkpoints."""
    for task in tasks:
        yield task


class ControlledAdapter(RecordingAdapter):
    """Track concurrent writers and block work until the test releases it."""

    def __init__(self) -> None:
        super().__init__()
        self.active_paths: set[Path] = set()
        self.peak = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    @override
    async def run_task(
        self,
        request: CodingTaskRequest,
        *,
        on_session_started: SessionStartedCallback | None = None,
    ) -> ProcessResult:
        assert request.cwd not in self.active_paths
        self.active_paths.add(request.cwd)
        self.peak = max(self.peak, len(self.active_paths))
        self.requests.append(request)
        if on_session_started is not None:
            await on_session_started(request.session_id or f"session-{len(self.requests)}")
        self.started.set()
        try:
            await self.release.wait()
            return ProcessResult(returncode=0)
        finally:
            self.active_paths.remove(request.cwd)


def executor(path: Path, adapter: RecordingAdapter) -> TaskExecutor:
    """Use real persistence with controlled provider and checkout boundaries."""
    database = path / "state.sqlite3"
    return TaskExecutor(
        ExecutionSettings(state_db_path=database),
        FakeVersionControl(),
        RunningSessionRepository(database),
        CronScheduleRepository(database),
        adapter_factory=lambda _: adapter,
    )


async def test_different_checkouts_run_concurrently_and_same_checkout_is_serial(
    tmp_path: Path,
) -> None:
    adapter = ControlledAdapter()
    scheduler = TaskScheduler(ExecutionSettings(max_active_tasks=2), executor(tmp_path, adapter))
    tasks = [
        issue_task(tmp_path / "first", 1),
        pull_request_task(tmp_path / "first", 2),
        pull_request_task(tmp_path / "second", 3),
    ]
    running = asyncio.create_task(scheduler.run(stream(tasks)))
    await asyncio.wait_for(adapter.started.wait(), 5)
    for _ in range(1000):
        if adapter.peak == 2:
            break
        await asyncio.sleep(0.01)
    assert adapter.peak == 2
    adapter.release.set()
    # Windows SQLite writers serialize through asyncio.to_thread; keep headroom.
    await asyncio.wait_for(running, 10)
    assert len(adapter.requests) == 3
    assert scheduler.failed_tasks == 0


async def test_cancellation_persists_and_resumes_original_session(tmp_path: Path) -> None:
    adapter = ControlledAdapter()
    configured = ExecutionSettings(max_active_tasks=1)
    scheduler = TaskScheduler(configured, executor(tmp_path, adapter))
    task = issue_task(tmp_path)
    running = asyncio.create_task(scheduler.run(stream([task])))
    await asyncio.wait_for(adapter.started.wait(), 1)
    running.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await running
    repository = RunningSessionRepository(tmp_path / "state.sqlite3")
    saved = await repository.list_all()
    assert len(saved) == 1
    assert saved[0].task == task
    resumed = RecordingAdapter()
    await TaskScheduler(configured, executor(tmp_path, resumed)).run(
        stream([task]), resume_sessions=saved
    )
    assert len(resumed.requests) == 1
    assert resumed.requests[0].session_id == saved[0].session_id
    assert await repository.list_all() == []


async def test_nonzero_exits_are_observed_without_stopping_other_tasks(tmp_path: Path) -> None:
    adapter = RecordingAdapter(returncode=7)
    scheduler = TaskScheduler(ExecutionSettings(), executor(tmp_path, adapter))
    await scheduler.run(stream([issue_task(tmp_path, 1), issue_task(tmp_path, 2)]))
    assert scheduler.failed_tasks == 2


async def test_linked_issue_recovery_is_retired_without_starting_an_agent(
    tmp_path: Path,
) -> None:
    task = issue_task(tmp_path)
    session = RunningCodingSession(task=task, session_id="stale", message="old prompt")
    database = tmp_path / "state.sqlite3"
    sessions = RunningSessionRepository(database)
    await sessions.save(session)
    adapter = RecordingAdapter()
    github = FakeGitHub(
        issues=[GhIssue(number=42, title="Task 42", url="https://github.com/acme/api/issues/42")],
        pulls=[
            GhPullRequest(
                number=12,
                title="Implements #42",
                body="Closes #42",
                url="https://github.com/acme/api/pull/12",
            )
        ],
    )
    worker = TaskExecutor(
        ExecutionSettings(state_db_path=database),
        FakeVersionControl(),
        sessions,
        CronScheduleRepository(database),
        adapter_factory=lambda _: adapter,
    )
    scheduler = TaskScheduler(
        ExecutionSettings(), worker, task_validator=GitHubTaskRevalidator(github)
    )

    await scheduler.run(stream([]), resume_sessions=[session])

    assert adapter.requests == []
    assert await sessions.list_all() == []


async def test_same_logical_pr_is_only_run_once_across_automations(tmp_path: Path) -> None:
    adapter = RecordingAdapter()
    tasks = [
        pull_request_task(tmp_path / "first", 12, "first").model_copy(
            update={"head_sha": "same-head"}
        ),
        pull_request_task(tmp_path / "second", 12, "second").model_copy(
            update={"head_sha": "same-head"}
        ),
    ]
    scheduler = TaskScheduler(ExecutionSettings(max_active_tasks=2), executor(tmp_path, adapter))

    await scheduler.run(stream([]), initial_tasks=tasks)

    assert len(adapter.requests) == 1


async def test_recovered_pr_with_new_head_drops_stale_session_and_starts_fresh(
    tmp_path: Path,
) -> None:
    stale = pull_request_task(tmp_path, 12).model_copy(
        update={"is_draft": False, "head_sha": "old-head"}
    )
    session = RunningCodingSession(task=stale, session_id="old-session", message="old review")
    database = tmp_path / "state.sqlite3"
    sessions = RunningSessionRepository(database)
    await sessions.save(session)
    adapter = RecordingAdapter()
    github = FakeGitHub(
        pulls=[
            GhPullRequest(
                number=12,
                title="Review",
                url="https://github.com/acme/api/pull/12",
                state="OPEN",
                isDraft=False,
                headRefOid="new-head",
            )
        ]
    )
    worker = TaskExecutor(
        ExecutionSettings(state_db_path=database),
        FakeVersionControl(),
        sessions,
        CronScheduleRepository(database),
        adapter_factory=lambda _: adapter,
    )
    scheduler = TaskScheduler(
        ExecutionSettings(), worker, task_validator=GitHubTaskRevalidator(github)
    )

    await scheduler.run(stream([]), resume_sessions=[session])

    assert len(adapter.requests) == 1
    assert adapter.requests[0].session_id is None
    assert adapter.requests[0].message != "old review"
    assert await sessions.list_all() == []


async def test_recovered_pr_that_moved_to_ready_stage_starts_a_fresh_task(
    tmp_path: Path,
) -> None:
    stale = pull_request_task(tmp_path, 12).model_copy(
        update={
            "is_draft": False,
            "head_sha": "same-head",
            "mergeable": "UNKNOWN",
            "merge_state_status": "UNKNOWN",
        }
    )
    session = RunningCodingSession(task=stale, session_id="old-review", message="stale prompt")
    database = tmp_path / "state.sqlite3"
    sessions = RunningSessionRepository(database)
    await sessions.save(session)
    adapter = RecordingAdapter()
    github = FakeGitHub(
        pulls=[
            GhPullRequest(
                number=12,
                title="Review",
                url="https://github.com/acme/api/pull/12",
                state="OPEN",
                isDraft=False,
                headRefOid="same-head",
                mergeable="MERGEABLE",
                mergeStateStatus="CLEAN",
                statusCheckRollup=[{"conclusion": "SUCCESS"}],
            )
        ]
    )
    worker = TaskExecutor(
        ExecutionSettings(state_db_path=database),
        FakeVersionControl(),
        sessions,
        CronScheduleRepository(database),
        adapter_factory=lambda _: adapter,
    )
    scheduler = TaskScheduler(
        ExecutionSettings(), worker, task_validator=GitHubTaskRevalidator(github)
    )

    await scheduler.run(stream([]), resume_sessions=[session])

    assert len(adapter.requests) == 1
    assert adapter.requests[0].session_id is None
    assert adapter.requests[0].message != "stale prompt"
    assert await sessions.list_all() == []


async def test_closed_issue_recovery_is_retired_without_starting_an_agent(
    tmp_path: Path,
) -> None:
    task = issue_task(tmp_path)
    session = RunningCodingSession(task=task, session_id="closed", message="old prompt")
    database = tmp_path / "state.sqlite3"
    sessions = RunningSessionRepository(database)
    await sessions.save(session)
    adapter = RecordingAdapter()
    github = FakeGitHub(
        issues=[
            GhIssue(
                number=42,
                title="Task 42",
                url="https://github.com/acme/api/issues/42",
                state="CLOSED",
            )
        ]
    )
    worker = TaskExecutor(
        ExecutionSettings(state_db_path=database),
        FakeVersionControl(),
        sessions,
        CronScheduleRepository(database),
        adapter_factory=lambda _: adapter,
    )
    scheduler = TaskScheduler(
        ExecutionSettings(), worker, task_validator=GitHubTaskRevalidator(github)
    )

    await scheduler.run(stream([]), resume_sessions=[session])

    assert adapter.requests == []
    assert await sessions.list_all() == []


async def test_merged_pull_request_recovery_is_retired(tmp_path: Path) -> None:
    task = pull_request_task(tmp_path, 12)
    session = RunningCodingSession(task=task, session_id="merged", message="old review")
    database = tmp_path / "state.sqlite3"
    sessions = RunningSessionRepository(database)
    await sessions.save(session)
    adapter = RecordingAdapter()
    github = FakeGitHub(
        pulls=[
            GhPullRequest(
                number=12,
                title="Review",
                url="https://github.com/acme/api/pull/12",
                state="CLOSED",
                mergedAt="2026-10-10T12:00:00Z",
            )
        ]
    )
    worker = TaskExecutor(
        ExecutionSettings(state_db_path=database),
        FakeVersionControl(),
        sessions,
        CronScheduleRepository(database),
        adapter_factory=lambda _: adapter,
    )
    scheduler = TaskScheduler(
        ExecutionSettings(), worker, task_validator=GitHubTaskRevalidator(github)
    )

    await scheduler.run(stream([]), resume_sessions=[session])

    assert adapter.requests == []
    assert await sessions.list_all() == []


async def test_completed_issue_is_not_repeated_until_its_source_snapshot_changes(
    tmp_path: Path,
) -> None:
    adapter = RecordingAdapter()
    database = tmp_path / "state.sqlite3"
    configured = ExecutionSettings(state_db_path=database)
    completed = CompletedTaskRepository(database)
    task = issue_task(tmp_path)

    async def next_action(_: Task) -> str:
        return "Create one draft pull request with a closing reference."

    async def run(selected: Task) -> None:
        await TaskScheduler(
            configured,
            executor(tmp_path, adapter),
            completion_verifier=next_action,
            completed_tasks=completed,
        ).run(stream([]), initial_tasks=[selected])

    await run(task)
    state = await completed.get(task)
    assert state is not None
    assert state.next_action == "Create one draft pull request with a closing reference."

    await run(task)
    assert len(adapter.requests) == 1

    await run(task.model_copy(update={"title": "Updated issue"}))
    assert len(adapter.requests) == 2


async def test_completed_pr_with_changed_head_is_re_admitted(tmp_path: Path) -> None:
    adapter = RecordingAdapter()
    database = tmp_path / "state.sqlite3"
    configured = ExecutionSettings(state_db_path=database)
    completed = CompletedTaskRepository(database)
    first = pull_request_task(tmp_path, 12).model_copy(update={"head_sha": "head-1"})
    changed = first.model_copy(update={"head_sha": "head-2"})

    async def next_action(_: Task) -> str:
        return "Recheck the current PR head and remote checks."

    for task in (first, changed):
        await TaskScheduler(
            configured,
            executor(tmp_path, adapter),
            completion_verifier=next_action,
            completed_tasks=completed,
        ).run(stream([]), initial_tasks=[task])

    assert len(adapter.requests) == 2


async def test_scheduler_orders_initial_tasks_by_stage(tmp_path: Path) -> None:
    adapter = RecordingAdapter()
    issue = issue_task(tmp_path, 7)
    draft = pull_request_task(tmp_path, 8)
    ready = pull_request_task(tmp_path, 9).model_copy(
        update={
            "is_draft": False,
            "mergeable": "MERGEABLE",
            "merge_state_status": "CLEAN",
            "check_conclusions": ("SUCCESS",),
            "head_sha": "head-9",
        }
    )
    scheduler = TaskScheduler(ExecutionSettings(), executor(tmp_path, adapter))

    await scheduler.run(stream([]), initial_tasks=[issue, draft, ready])

    assert [request.message for request in adapter.requests] == [
        "Handle github-cli-pull-requests 9: Review",
        "Handle github-cli-pull-requests 8: Review",
        "Handle issue 7: Task 7",
    ]
