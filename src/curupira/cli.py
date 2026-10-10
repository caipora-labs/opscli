"""Command-line entry points for configuration, one-shot dispatch, and polling."""

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Annotated, Literal

import typer
from pydantic import ValidationError

from curupira import __version__
from curupira.clients.gh import GhClient
from curupira.config import ApplicationSettings, load_settings
from curupira.dispatcher import create_task_feeds, dispatch_next_task
from curupira.errors import DispatchError
from curupira.executor import TaskExecutor
from curupira.models import Task
from curupira.models.base import ValidatedModel
from curupira.runtime import (
    DispatchInstanceLock,
    InstanceAlreadyRunningError,
    create_execution_log_handler,
    default_config_path,
    dispatch_home,
    ensure_runtime_directories,
)
from curupira.scheduler import TaskScheduler
from curupira.status import TerminalTaskStatus
from curupira.storage import (
    CompletedTaskRepository,
    CronScheduleRepository,
    RunningSessionRepository,
)
from curupira.tasks.base import TaskFeed
from curupira.tasks.feed import poll_task_feeds, prioritize_task_stream
from curupira.tasks.revalidation import GitHubTaskRevalidator
from curupira.telemetry import TaskTelemetry
from curupira.vcs.base import VersionControl


class CliOptions(ValidatedModel):
    """Validated command-line options.

    Attributes:
        command: Selected Curupira subcommand.
        config: Path to the TOML configuration file.
        dry_run: When true, preview one task without reserving or executing.
        watch: When true, poll continuously instead of draining available work.
        size: Optional admission cap for a finite ``run`` drain.
    """

    command: Literal["validate", "run", "tui"]
    config: Path
    dry_run: bool = False
    watch: bool = False
    size: int | None = None


_CONSOLE_SCRIPTS = frozenset({"curupira", "curu"})
_DISPATCH_COMMANDS = frozenset({"run", "tui"})


def _program_name(argv0: str | None = None) -> str:
    """Return ``curupira`` or ``curu`` when that console script was invoked."""
    raw = sys.argv[0] if argv0 is None else argv0
    name = Path(raw).name
    if name.lower().endswith(".exe"):
        name = name[: -len(".exe")]
    canonical = name.lower()
    if canonical in _CONSOLE_SCRIPTS:
        return canonical
    return "curupira"


def _version_callback(ctx: typer.Context, value: bool) -> None:
    """Print the package version and exit when ``--version`` is set."""
    if value:
        name = ctx.find_root().info_name or _program_name()
        typer.echo(f"{name} {__version__}")
        raise typer.Exit(0)


def _positive_int(value: int) -> int:
    """Reject non-positive batch sizes with a Typer-friendly error."""
    if value < 1:
        raise typer.BadParameter("must be a positive integer")
    return value


def _optional_positive_int(value: int | None) -> int | None:
    """Allow omitted ``--size`` while still validating positive integers."""
    if value is None:
        return None
    return _positive_int(value)


app = typer.Typer(
    help="Dispatch GitHub and cron automations to native coding-agent CLIs.",
    no_args_is_help=True,
    add_completion=False,
    # Keep classic Click help so scripts and tests can match plain "Usage:" text.
    rich_markup_mode=None,
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def _root(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="TOML configuration file (default: ~/.curupira/settings.toml)",
            show_default=False,
        ),
    ] = None,
    version: Annotated[
        bool | None,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = None,
) -> None:
    """Shared options for every Curupira subcommand."""
    del version
    ctx.ensure_object(dict)
    ctx.obj["config"] = config if config is not None else default_config_path()


def _exit_with(options: CliOptions) -> None:
    """Run the async command path and translate its status into ``typer.Exit``."""
    raise typer.Exit(_run(options))


def _run(options: CliOptions) -> int:
    """Own the event-loop lifecycle for a validated command."""
    try:
        return asyncio.run(async_main(options))
    except KeyboardInterrupt:
        return 130


@app.command("validate")
def validate_command(ctx: typer.Context) -> None:
    """Validate configuration without calling external CLIs or writing state."""
    _exit_with(CliOptions(command="validate", config=ctx.obj["config"]))


@app.command("run")
def run_command(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Preview a task without reserving, persisting, cloning, or executing",
        ),
    ] = False,
    watch: Annotated[
        bool,
        typer.Option(
            "--watch",
            help="Poll all automations continuously until interrupted",
        ),
    ] = False,
    size: Annotated[
        int | None,
        typer.Option(
            "--size",
            help="Process at most N tasks (finite drain only)",
            callback=_optional_positive_int,
        ),
    ] = None,
) -> None:
    """Drain currently available automation tasks, or watch continuously with ``--watch``."""
    if watch and dry_run:
        raise typer.BadParameter("--dry-run cannot be combined with --watch")
    if watch and size is not None:
        raise typer.BadParameter("--size cannot be combined with --watch")
    if dry_run and size is not None:
        raise typer.BadParameter("--size cannot be combined with --dry-run")
    _exit_with(
        CliOptions(
            command="run",
            config=ctx.obj["config"],
            dry_run=dry_run,
            watch=watch,
            size=size,
        )
    )


@app.command("tui")
def tui_command(ctx: typer.Context) -> None:
    """Run the orchestrator with an interactive terminal dashboard."""
    _exit_with(CliOptions(command="tui", config=ctx.obj["config"]))


plugins_app = typer.Typer(
    help="Inspect trigger and coding-agent plugins.", no_args_is_help=True, rich_markup_mode=None
)
app.add_typer(plugins_app, name="plugins")


@plugins_app.command("list")
def plugins_list_command() -> None:
    """List every registered trigger type and agent provider and their distributions."""
    try:
        lines = [*describe_triggers(), *describe_agents()]
    except DispatchError as error:
        typer.echo(f"Plugin error: {error}", err=True)
        raise typer.Exit(1) from error
    for line in lines:
        typer.echo(line)


def describe_triggers() -> list[str]:
    """Describe built-in and plugin triggers with their prompt placeholders."""
    from curupira.plugins import loaded_plugins
    from curupira.tasks.registry import registered

    origins = {
        plugin.trigger_type: f"{plugin.distribution} {plugin.version}"
        for plugin in loaded_plugins()
    }
    lines: list[str] = []
    for trigger_type, trigger in registered().items():
        origin = origins.get(trigger_type, f"curupira {__version__} (built-in)")
        fields = ", ".join(sorted(trigger.prompt_fields())) or "-"
        lines.append(f"{trigger_type}\t{origin}\tprompt fields: {fields}")
    return lines


def describe_agents() -> list[str]:
    """Describe built-in and plugin coding-agent providers with their executables."""
    from curupira.agents.registry import registered
    from curupira.plugins import loaded_agent_plugins

    origins = {
        plugin.provider: f"{plugin.distribution} {plugin.version}"
        for plugin in loaded_agent_plugins()
    }
    return [
        f"agent:{provider}\t"
        f"{origins.get(provider, f'curupira {__version__} (built-in)')}\t"
        f"executable: {adapter.executable}"
        for provider, adapter in registered().items()
    ]


async def _batch_stream(feeds: Sequence[TaskFeed], size: int | None) -> AsyncIterator[Task]:
    """Poll feeds in common rounds until empty, capping stage-ordered admissions."""
    admitted = 0
    while size is None or admitted < size:
        tasks = await poll_task_feeds(feeds)
        if not tasks:
            return
        for task in tasks:
            if size is not None and admitted >= size:
                return
            admitted += 1
            yield task


def _limit_initial_tasks(tasks: Sequence[Task], size: int | None) -> tuple[list[Task], int | None]:
    """Apply a finite drain's cap to its initial poll and return the remaining allowance."""
    if size is None:
        return list(tasks), None
    admitted = list(tasks[:size])
    return admitted, size - len(admitted)


async def async_main(options: CliOptions) -> int:
    """Load validated settings and execute the selected CLI command."""
    instance_lock: DispatchInstanceLock | None = None
    try:
        config_path = options.config.expanduser().resolve()
        if config_path == default_config_path().resolve():
            config_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if options.command in _DISPATCH_COMMANDS and not options.dry_run:
            instance_lock = DispatchInstanceLock(dispatch_home() / "dispatch.lock")
            instance_lock.acquire()
            ensure_runtime_directories()
    except InstanceAlreadyRunningError as error:
        print(f"Dispatch error: {error}", file=sys.stderr)
        if instance_lock is not None:
            instance_lock.release()
        return 1
    except OSError as error:
        print(f"Runtime setup error: {error}", file=sys.stderr)
        if instance_lock is not None:
            instance_lock.release()
        return 1

    try:
        return await _execute_command(options)
    finally:
        if instance_lock is not None:
            instance_lock.release()


async def _execute_command(options: CliOptions) -> int:
    """Run a validated command after acquiring any required process lock."""
    try:
        settings = await load_settings(options.config)
    except (OSError, ValueError, DispatchError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    if options.command == "validate":
        count = len(settings.coding_agents.automations)
        limit = settings.settings.max_active_tasks
        print(f"Configuration is valid ({count} automations; max active tasks: {limit}).")
        return 0
    telemetry = TaskTelemetry(
        str(settings.settings.otlp_endpoint)
        if settings.settings.otlp_endpoint is not None
        else None
    )
    log_handler: logging.FileHandler | None = None
    root_logger = logging.getLogger()
    try:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        if options.command == "tui" or not options.dry_run:
            log_handler = create_execution_log_handler()
            root_logger.setLevel(logging.INFO)
            root_logger.addHandler(log_handler)
        gh = GhClient()
        if options.command == "run" and options.dry_run:
            return await _execute_dry_run(settings, gh, telemetry)
        if options.command == "tui":
            from curupira.tui.app import run_orchestrator_tui

            return await run_orchestrator_tui(settings, gh, None, telemetry)
        return await _execute_scheduled_command(settings, gh, None, telemetry, options)
    except (DispatchError, OSError, ValidationError) as error:
        print(f"Dispatch error: {error}", file=sys.stderr)
        return 1
    finally:
        try:
            telemetry.shutdown()
        finally:
            if log_handler is not None:
                root_logger.removeHandler(log_handler)
                log_handler.close()


async def _execute_dry_run(
    settings: ApplicationSettings,
    gh: GhClient,
    telemetry: TaskTelemetry,
) -> int:
    """Preview one currently available task without reserving or executing it."""
    status = TerminalTaskStatus()
    try:
        outcome = await dispatch_next_task(
            settings,
            gh,
            dry_run=True,
            telemetry=telemetry,
            on_task_selected=lambda task: status.update(
                (task,), settings.settings.max_active_tasks
            ),
        )
    finally:
        status.clear()
    if outcome.selected is None:
        print("No matching task is currently available.")
        return 0
    selected = outcome.selected
    identity = selected.identity
    print(f"Selected {identity.automation_id}: {identity.repo}#{identity.id}: {selected.title}")
    print(selected.url)
    print("Dry run: no task was reserved or executed.")
    return 0


async def _execute_scheduled_command(
    settings: ApplicationSettings,
    gh: GhClient,
    version_control: VersionControl | None,
    telemetry: TaskTelemetry,
    options: CliOptions,
) -> int:
    """Execute a finite drain or continuous watch through the shared scheduler."""
    sessions = RunningSessionRepository(settings.settings.state_db_path)
    completed_tasks = CompletedTaskRepository(settings.settings.state_db_path)
    recovered = await sessions.list_all()
    cron = CronScheduleRepository(settings.settings.state_db_path)
    feeds = create_task_feeds(settings, gh, cron)
    initial_tasks, remaining = _limit_initial_tasks(await poll_task_feeds(feeds), options.size)
    revalidate = GitHubTaskRevalidator(gh)
    executor = TaskExecutor(
        settings.settings,
        version_control,
        sessions,
        cron,
        telemetry=telemetry,
        pre_start_validator=revalidate,
    )
    status = TerminalTaskStatus(show_idle=True)
    scheduler = TaskScheduler(
        settings.settings,
        executor,
        on_active_tasks_changed=lambda tasks: status.update(
            tasks, settings.settings.max_active_tasks
        ),
        task_validator=revalidate,
        completion_verifier=revalidate.completion_next_action,
        completed_tasks=completed_tasks,
    )
    if options.watch:
        tasks = prioritize_task_stream(feeds, settings.settings.polling.poll_interval_seconds)
    else:
        tasks = _batch_stream(feeds, remaining)
    try:
        await scheduler.run(tasks, resume_sessions=recovered, initial_tasks=initial_tasks)
    finally:
        status.clear()
    return 1 if scheduler.failed_tasks else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and own the application's event-loop lifecycle."""
    try:
        result = app(
            args=list(argv) if argv is not None else None,
            prog_name=_program_name(),
            standalone_mode=False,
        )
    except typer.Exit as error:
        code = error.exit_code
        return 0 if code is None else code
    except SystemExit as error:
        code = error.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 1
    if result is None:
        return 0
    if isinstance(result, int):
        return result
    return 1
