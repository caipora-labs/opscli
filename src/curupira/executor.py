"""The shared checkout, prompt, execution, and persistence lifecycle."""

import logging
from asyncio import CancelledError
from collections.abc import Awaitable, Callable
from string import Template

from curupira.agents import CliAdapterFactory, create_cli_adapter
from curupira.agents.base import (
    RESUME_SESSION_PROMPT,
    CodingAgentCliAdapter,
)
from curupira.clients.process import AsyncProcessRunner
from curupira.errors import PromptRenderError
from curupira.models import (
    CodingTaskRequest,
    ExecutionSettings,
    ProcessResult,
    RunningCodingSession,
    Task,
)
from curupira.storage import CronScheduleRepository, RunningSessionRepository
from curupira.tasks.base import TriggerState
from curupira.tasks.registry import get as get_trigger
from curupira.telemetry import TaskTelemetry
from curupira.vcs.base import CheckoutRequest, VersionControl
from curupira.vcs.github_cli import GitHubCliVersionControl

logger = logging.getLogger(__name__)


def render_task_prompt(task: Task) -> str:
    """Render only the task's own template using common and source-specific fields."""
    identity = task.identity
    context: dict[str, str] = {
        "repo": identity.repo,
        "automation_id": identity.automation_id,
        "task_type": identity.task_type,
        "task_number": identity.id,
        "task_title": task.title,
        "task_body": task.body or "",
        "task_url": task.url,
    }
    context.update(get_trigger(identity.task_type).prompt_context(task))
    try:
        return Template(task.automation.configuration.prompt).substitute(context)
    except (KeyError, ValueError) as error:
        raise PromptRenderError(
            f"could not render prompt for {identity.automation_id}: {error}"
        ) from error


class TaskExecutor:
    """Execute new or resumed tasks with identical provider and state semantics."""

    def __init__(
        self,
        settings: ExecutionSettings,
        version_control: VersionControl | None,
        sessions: RunningSessionRepository,
        cron: CronScheduleRepository,
        *,
        adapter_factory: CliAdapterFactory = create_cli_adapter,
        telemetry: TaskTelemetry | None = None,
        runner: AsyncProcessRunner | None = None,
        pre_start_validator: Callable[[Task], Awaitable[Task | None]] | None = None,
    ) -> None:
        self._settings = settings
        self._runner = runner or AsyncProcessRunner()
        self._default_version_control = version_control or GitHubCliVersionControl(self._runner)
        self._version_controls: dict[str, VersionControl] = {}
        self._sessions = sessions
        self._state = TriggerState(sessions=sessions, cron=cron)
        self._adapter_factory = adapter_factory
        self._telemetry = telemetry or TaskTelemetry()
        self._adapters: dict[str, CodingAgentCliAdapter] = {}
        self._pre_start_validator = pre_start_validator

    async def execute(
        self, task: Task, resumed: RunningCodingSession | None = None
    ) -> ProcessResult:
        """Execute one task within an outcome span."""
        identity = task.identity
        context = (identity.repo, identity.task_type, identity.id)
        action = "Resuming" if resumed is not None else "Starting"
        logger.info("%s task repo=%s type=%s id=%s", action, *context)
        try:
            with self._telemetry.task_span(task) as span:
                result = await self._execute_task(task, resumed)
                self._telemetry.record_result(span, result)
        except CancelledError:
            logger.warning("Cancelled task repo=%s type=%s id=%s", *context)
            raise
        except Exception as error:
            logger.exception(
                "Failed task repo=%s type=%s id=%s result=failure error=%s",
                *context,
                str(error) or type(error).__name__,
            )
            raise

        if result.returncode == 0:
            logger.info("Completed task repo=%s type=%s id=%s result=success", *context)
        else:
            message = result.stderr.strip() or f"process exited with status {result.returncode}"
            logger.error(
                "Failed task repo=%s type=%s id=%s result=failure error=%s",
                *context,
                message,
            )
        return result

    async def _execute_task(
        self, task: Task, resumed: RunningCodingSession | None = None
    ) -> ProcessResult:
        """Persist session events and remove state only after the native process exits."""
        if resumed is not None and resumed.task != task:
            raise ValueError("resumption must use the original persisted task snapshot")
        profile = task.automation.profile
        provider = profile.provider
        adapter = self._adapters.get(provider)
        if adapter is None:
            adapter = self._adapter_factory(provider)
            self._adapters[provider] = adapter
        trigger = get_trigger(task.identity.task_type)
        version_control = self._version_control_for(task)
        checkout = await version_control.ensure_checkout(
            CheckoutRequest(repo=task.identity.repo, destination=task.automation.workspace_path)
        )
        setup_script = task.automation.configuration.setup_script
        if checkout.cloned and setup_script is not None:
            result = await version_control.run_setup_script(
                checkout,
                setup_script,
                timeout_seconds=self._settings.task_timeout_seconds,
                max_output_bytes=self._settings.max_output_bytes,
            )
            if result.returncode:
                logger.error(
                    "Setup script failed path=%s code=%s stderr=%s",
                    setup_script,
                    result.returncode,
                    result.stderr,
                )
                return result
        if self._pre_start_validator is not None:
            current = await self._pre_start_validator(task)
            if current is None or current.state_fingerprint != task.state_fingerprint:
                await self._sessions.delete(task)
                logger.info(
                    "Task %s changed before agent start; it will be reconsidered from current "
                    "source state",
                    task.identity.key,
                )
                return ProcessResult(
                    returncode=0,
                    stdout=(
                        "Task changed before agent start; current source state will be rechecked."
                    ),
                )
        original_message = resumed.message if resumed is not None else render_task_prompt(task)

        async def persist(session_id: str) -> None:
            await self._sessions.save(
                RunningCodingSession(task=task, session_id=session_id, message=original_message)
            )

        await trigger.on_task_started(task, self._state)
        is_worktree = task.automation.configuration.checkout == "worktree"
        cwd = checkout.path
        if is_worktree:
            cwd = await version_control.ensure_worktree(
                checkout,
                automation_id=task.identity.automation_id,
                task_type=task.identity.task_type,
                task_id=task.identity.id,
            )
        try:
            result = await adapter.run_task(
                CodingTaskRequest(
                    cwd=cwd,
                    profile=profile,
                    message=RESUME_SESSION_PROMPT if resumed is not None else original_message,
                    session_id=resumed.session_id if resumed is not None else None,
                    timeout=self._settings.task_timeout_seconds,
                    max_output_bytes=self._settings.max_output_bytes,
                ),
                on_session_started=persist,
            )
            await trigger.on_task_finished(task, self._state)
            return result
        finally:
            if is_worktree:
                try:
                    await version_control.remove_worktree(
                        checkout,
                        automation_id=task.identity.automation_id,
                        task_type=task.identity.task_type,
                        task_id=task.identity.id,
                    )
                except Exception:
                    logger.exception("Could not clean up worktree for %s", task.identity.key)

    async def discard_session(self, task: Task) -> None:
        """Retire a recovered session that no longer matches current source state."""
        await self._sessions.delete(task)

    def _version_control_for(self, task: Task) -> VersionControl:
        """Return the trigger's clone mechanism, created once per trigger type."""
        task_type = task.identity.task_type
        version_control = self._version_controls.get(task_type)
        if version_control is None:
            created = get_trigger(task_type).create_version_control(self._runner)
            version_control = created or self._default_version_control
            self._version_controls[task_type] = version_control
        return version_control
