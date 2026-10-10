"""Feed construction and source-independent one-shot dispatch."""

import logging
from collections.abc import Callable

from curupira.agents import CliAdapterFactory, create_cli_adapter
from curupira.clients.az import AzClient
from curupira.clients.gh import GhClient
from curupira.config import ApplicationSettings
from curupira.executor import TaskExecutor
from curupira.models import (
    DispatchOutcome,
    Task,
)
from curupira.storage import (
    CompletedTaskRepository,
    CronScheduleRepository,
    RunningSessionRepository,
)
from curupira.tasks.base import FeedDependencies, TaskFeed
from curupira.tasks.feed import poll_task_feeds
from curupira.tasks.registry import get as get_trigger
from curupira.tasks.revalidation import GitHubTaskRevalidator
from curupira.telemetry import TaskTelemetry
from curupira.vcs.base import VersionControl

logger = logging.getLogger(__name__)


def create_task_feeds(
    settings: ApplicationSettings,
    gh: GhClient,
    cron: CronScheduleRepository,
    az: AzClient | None = None,
) -> list[TaskFeed]:
    """Build source-specific discovery using one resolved configuration snapshot."""
    azure = az or AzClient()
    feeds: list[TaskFeed] = []
    for automation in settings.resolve_automations().values():
        dependencies = FeedDependencies(
            polling=settings.settings.polling,
            gh=gh,
            az=azure,
            cron=cron,
            state_db_path=settings.settings.state_db_path,
        )
        feeds.append(
            get_trigger(automation.configuration.trigger_type).build_feed(automation, dependencies)
        )
    return feeds


async def dispatch_next_task(
    settings: ApplicationSettings,
    gh: GhClient,
    *,
    dry_run: bool = False,
    adapter_factory: CliAdapterFactory = create_cli_adapter,
    version_control: VersionControl | None = None,
    telemetry: TaskTelemetry | None = None,
    on_task_selected: Callable[[Task], None] | None = None,
    az: AzClient | None = None,
) -> DispatchOutcome:
    """Run the first currently available task, or preview it without any writes."""
    cron = CronScheduleRepository(settings.settings.state_db_path)
    feeds = create_task_feeds(settings, gh, cron, az)
    available = await poll_task_feeds(feeds, preview=dry_run)
    if dry_run:
        return DispatchOutcome(selected=available[0] if available else None)
    sessions = RunningSessionRepository(settings.settings.state_db_path)
    completed_tasks = CompletedTaskRepository(settings.settings.state_db_path)
    revalidate = GitHubTaskRevalidator(gh)
    executor = TaskExecutor(
        settings.settings,
        version_control,
        sessions,
        cron,
        adapter_factory=adapter_factory,
        telemetry=telemetry,
        pre_start_validator=revalidate,
    )
    for selected in available:
        current = await revalidate(selected)
        if current is None:
            await executor.discard_session(selected)
            await completed_tasks.delete(selected)
            continue
        completed = await completed_tasks.get(current)
        if completed is not None:
            if completed.fingerprint == current.state_fingerprint:
                await executor.discard_session(current)
                logger.info(
                    "Skipping unchanged completed task %s; next action: %s",
                    current.url,
                    completed.next_action,
                )
                continue
            await completed_tasks.delete(current)
        resumed = await sessions.get(current)
        if resumed is not None and resumed.task != current:
            await executor.discard_session(resumed.task)
            resumed = None
        task = current
        if on_task_selected is not None:
            on_task_selected(task)
        result = await executor.execute(task, resumed)
        if result.returncode == 0:
            try:
                next_action = await revalidate.completion_next_action(task)
            except Exception as error:
                next_action = (
                    "Retry GitHub state validation before dispatching this completed snapshot "
                    f"(verification error: {type(error).__name__})."
                )
                logger.error(
                    "Could not verify completion for %s; next action: %s", task.url, next_action
                )
            if next_action is None:
                await completed_tasks.delete(task)
            else:
                await completed_tasks.save(task, next_action)
                logger.info("Task %s remains incomplete; next action: %s", task.url, next_action)
        return DispatchOutcome(selected=task, process=result)
    return DispatchOutcome(selected=None)
