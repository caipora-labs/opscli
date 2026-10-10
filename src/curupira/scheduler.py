"""Bounded concurrent scheduling with exclusive execution per checkout."""

import asyncio
import logging
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path

from curupira.executor import TaskExecutor
from curupira.models import ExecutionSettings, ProcessResult, RunningCodingSession, Task
from curupira.storage import CompletedTaskRepository
from curupira.tasks.feed import task_order_key
from curupira.tasks.revalidation import TaskValidator

logger = logging.getLogger(__name__)
WorkItem = tuple[Task, RunningCodingSession | None]
ActiveTasks = dict[asyncio.Task[ProcessResult], Task]
ReaderTask = asyncio.Task[WorkItem | None]


class TaskScheduler:
    """Run independent workspaces concurrently without concurrent checkout writers."""

    def __init__(
        self,
        settings: ExecutionSettings,
        executor: TaskExecutor,
        *,
        on_active_tasks_changed: Callable[[Sequence[Task]], None] | None = None,
        task_validator: TaskValidator | None = None,
        completion_verifier: Callable[[Task], Awaitable[str | None]] | None = None,
        completed_tasks: CompletedTaskRepository | None = None,
    ) -> None:
        self._settings = settings
        self._executor = executor
        self._on_active_tasks_changed = on_active_tasks_changed
        self._task_validator = task_validator
        self._completion_verifier = completion_verifier
        self._completed_tasks = completed_tasks
        self._admit = asyncio.Event()
        self._admit.set()
        self.failed_tasks = 0

    def pause(self) -> None:
        """Stop admitting newly discovered work until ``resume`` is called."""
        self._admit.clear()

    def resume(self) -> None:
        """Allow the scheduler to admit and launch work again."""
        self._admit.set()

    @property
    def paused(self) -> bool:
        """Return whether new work admission is currently suspended."""
        return not self._admit.is_set()

    async def run(
        self,
        tasks: AsyncIterator[Task],
        *,
        resume_sessions: Sequence[RunningCodingSession] = (),
        initial_tasks: Sequence[Task] = (),
    ) -> None:
        """Consume ordered work while validating recovered and discovered task snapshots."""
        initial_items: list[WorkItem] = [(session.task, session) for session in resume_sessions]
        initial_items.extend((task, None) for task in initial_tasks)
        pending: deque[WorkItem] = deque(
            sorted(initial_items, key=lambda item: _work_order_key(item))
        )
        active: ActiveTasks = {}
        seen: set[str] = set()
        reader: ReaderTask | None = None

        async def incoming() -> AsyncGenerator[WorkItem, None]:
            try:
                async for selected in tasks:
                    yield selected, None
            finally:
                if isinstance(tasks, AsyncGenerator):
                    await tasks.aclose()

        source = incoming()
        exhausted = False
        try:
            self._notify_active_tasks(active)
            while not exhausted or pending or active or self.paused:
                if self._admit.is_set():
                    await self._launch_available(pending, active, seen)
                    reader = self._ensure_reader(reader, exhausted, pending, source)
                waiting: set[asyncio.Task[object]] = set(active)
                if reader is not None:
                    waiting.add(reader)
                if not waiting:
                    if self.paused and (not exhausted or pending):
                        await self._admit.wait()
                        continue
                    break
                finished, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                if reader is not None and reader in finished:
                    exhausted = self._consume_reader(reader, pending, seen)
                    reader = None
                await self._reap_finished(active, finished, seen)
                self._notify_active_tasks(active)
        finally:
            await self._cancel_all(reader, active, source)

    async def _launch_available(
        self, pending: deque[WorkItem], active: ActiveTasks, seen: set[str]
    ) -> None:
        """Start pending tasks while concurrency and checkout exclusivity allow it."""
        occupied: set[Path] = {item.automation.workspace_path for item in active.values()}
        active_logical = {item.logical_key for item in active.values()}
        validated = await self._validate_pending(pending, seen)
        for selected, resumed in validated:
            path = selected.automation.workspace_path
            if selected.automation.configuration.checkout == "worktree":
                path = path.with_name(f"{path.name}.worktrees") / selected.identity.key
            if (
                len(active) >= self._settings.max_active_tasks
                or path in occupied
                or selected.logical_key in active_logical
            ):
                pending.append((selected, resumed))
                continue
            seen.add(selected.dispatch_key)
            active_logical.add(selected.logical_key)
            worker = asyncio.create_task(
                self._executor.execute(selected, resumed), name=selected.identity.key
            )
            active[worker] = selected
            occupied.add(path)
            self._notify_active_tasks(active)

    async def _validate_pending(self, pending: deque[WorkItem], seen: set[str]) -> list[WorkItem]:
        """Revalidate, retire stale sessions, and deduplicate one pending admission round."""
        candidates = sorted(pending, key=_work_order_key)
        pending.clear()
        queued_keys = set(seen)
        validated: list[WorkItem] = []
        for item in candidates:
            refreshed = await self._validate_candidate(item, seen)
            if refreshed is None:
                continue
            selected, resumed = refreshed
            key = selected.dispatch_key
            if key in queued_keys:
                if resumed is not None:
                    await self._executor.discard_session(resumed.task)
                continue
            queued_keys.add(key)
            validated.append(refreshed)
        return sorted(validated, key=_work_order_key)

    async def _validate_candidate(self, item: WorkItem, seen: set[str]) -> WorkItem | None:
        """Refresh one candidate and retire any session or result for an outdated snapshot."""
        selected, resumed = item
        original = selected
        if self._task_validator is not None:
            selected = await self._task_validator(selected)
            if selected is None:
                if resumed is not None:
                    await self._executor.discard_session(resumed.task)
                if self._completed_tasks is not None:
                    await self._completed_tasks.delete(original)
                seen.add(original.dispatch_key)
                return None
        if resumed is not None and resumed.task != selected:
            await self._executor.discard_session(resumed.task)
            resumed = None
        if self._completed_tasks is not None:
            completed = await self._completed_tasks.get(selected)
            if completed is not None:
                if completed.fingerprint == selected.state_fingerprint:
                    if resumed is not None:
                        await self._executor.discard_session(resumed.task)
                    logger.info(
                        "Skipping unchanged completed task %s; next action: %s",
                        selected.url,
                        completed.next_action,
                    )
                    seen.add(selected.dispatch_key)
                    return None
                await self._completed_tasks.delete(selected)
        return selected, resumed

    def _notify_active_tasks(self, active: ActiveTasks) -> None:
        """Publish active task snapshots for terminal status rendering."""
        if self._on_active_tasks_changed is not None:
            self._on_active_tasks_changed(tuple(active.values()))

    def _ensure_reader(
        self,
        reader: ReaderTask | None,
        exhausted: bool,
        pending: deque[WorkItem],
        source: AsyncGenerator[WorkItem, None],
    ) -> ReaderTask | None:
        """Read the next work item in the background until the stream is bounded or exhausted."""
        if reader is None and not exhausted and len(pending) < self._settings.max_pending_tasks:
            return asyncio.create_task(anext(source, None))
        return reader

    @staticmethod
    def _consume_reader(reader: ReaderTask, pending: deque[WorkItem], seen: set[str]) -> bool:
        """Admit a discovered item unless its identity is already tracked; report exhaustion."""
        item = reader.result()
        if item is None:
            return True
        key = item[0].dispatch_key
        if key not in seen:
            pending.append(item)
        return False

    async def _reap_finished(
        self, active: ActiveTasks, finished: set[asyncio.Task[object]], seen: set[str]
    ) -> None:
        """Account for completed workers and release cron keys for re-scheduling."""
        for worker in list(active):
            if worker not in finished:
                continue
            selected = active.pop(worker)
            result = self._record_outcome(selected, worker)
            if (
                result is not None
                and result.returncode == 0
                and self._completion_verifier is not None
            ):
                try:
                    next_action = await self._completion_verifier(selected)
                except Exception as error:
                    self.failed_tasks += 1
                    next_action = (
                        "Retry GitHub state validation before dispatching this completed snapshot "
                        f"(verification error: {type(error).__name__})."
                    )
                    if self._completed_tasks is not None:
                        await self._completed_tasks.save(selected, next_action)
                    logger.error(
                        "Could not verify completion for %s; next action: %s",
                        selected.url,
                        next_action,
                    )
                else:
                    if next_action is None:
                        if self._completed_tasks is not None:
                            await self._completed_tasks.delete(selected)
                    else:
                        if self._completed_tasks is not None:
                            await self._completed_tasks.save(selected, next_action)
                        logger.info(
                            "Task %s remains incomplete; next action: %s", selected.url, next_action
                        )
            if selected.identity.task_type == "cron":
                seen.discard(selected.dispatch_key)

    def _record_outcome(
        self, selected: Task, worker: asyncio.Task[ProcessResult]
    ) -> ProcessResult | None:
        """Count and log a completed task that exited unsuccessfully."""
        try:
            result = worker.result()
            if result.returncode != 0:
                self.failed_tasks += 1
                logger.error(
                    "Task %s failed with exit %s", selected.identity.key, result.returncode
                )
            return result
        except Exception:
            self.failed_tasks += 1
            logger.exception("Task %s failed", selected.identity.key)
            return None

    @staticmethod
    async def _cancel_all(
        reader: ReaderTask | None, active: ActiveTasks, source: AsyncGenerator[WorkItem, None]
    ) -> None:
        """Cancel outstanding work and close the input stream."""
        if reader is not None:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        for worker in active:
            worker.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        await source.aclose()


def _work_order_key(item: WorkItem) -> tuple[int, str, str, int, str]:
    """Order a pending task by stage, preferring a matching recoverable session."""
    task, resumed = item
    return (*task_order_key(task)[:3], 0 if resumed is not None else 1, task.identity.automation_id)
