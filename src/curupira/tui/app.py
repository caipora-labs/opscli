"""Textual application that runs the watch pipeline inside a live dashboard."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import ClassVar

import psutil
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, RichLog, Static
from typing_extensions import override

from curupira.clients.gh import GhClient
from curupira.config import ApplicationSettings
from curupira.dispatcher import create_task_feeds
from curupira.executor import TaskExecutor
from curupira.models import Task
from curupira.scheduler import TaskScheduler
from curupira.storage import (
    CompletedTaskRepository,
    CronScheduleRepository,
    RunningSessionRepository,
)
from curupira.tasks.feed import poll_task_feeds, prioritize_task_stream
from curupira.tasks.revalidation import GitHubTaskRevalidator
from curupira.telemetry import TaskTelemetry
from curupira.tui.logging_handler import TuiLogHandler, attach_rich_log
from curupira.tui.status import OrchestratorStatus
from curupira.tui.widgets import AgentsPanel, LogsPanel, MetricsPanel
from curupira.vcs.base import VersionControl


class HelpScreen(ModalScreen[None]):
    """Simple modal listing keyboard shortcuts."""

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen #help-box {
        width: 60;
        height: auto;
        border: solid #c8c8c8;
        background: #101010;
        padding: 1 2;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_help", "Fechar", show=False),
        Binding("f1", "dismiss_help", "Fechar", show=False),
    ]

    @override
    def compose(self) -> ComposeResult:
        """Render shortcut help text."""
        yield Static(
            "[b]Ajuda — Orquestrador Curupira[/b]\n\n"
            "Ctrl+C  Sair\n"
            "F1      Esta ajuda\n"
            "F2      Pausar / retomar admissão de tarefas\n"
            "F3      Mostrar resumo da configuração\n"
            "F5      Atualizar métricas do sistema\n\n"
            "Pressione Esc ou F1 para fechar.",
            id="help-box",
            markup=True,
        )

    def action_dismiss_help(self) -> None:
        """Close the help modal."""
        self.dismiss(None)


class ConfigScreen(ModalScreen[None]):
    """Modal summarizing loaded configuration paths and limits."""

    DEFAULT_CSS = """
    ConfigScreen {
        align: center middle;
    }
    ConfigScreen #config-box {
        width: 72;
        height: auto;
        border: solid #c8c8c8;
        background: #101010;
        padding: 1 2;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss_config", "Fechar", show=False),
        Binding("f3", "dismiss_config", "Fechar", show=False),
    ]

    def __init__(self, summary: str) -> None:
        super().__init__()
        self._summary = summary

    @override
    def compose(self) -> ComposeResult:
        """Render the configuration summary."""
        yield Static(self._summary, id="config-box", markup=True)

    def action_dismiss_config(self) -> None:
        """Close the config modal."""
        self.dismiss(None)


class OrchestratorApp(App[int]):
    """Watch-mode orchestrator with metrics, agents, and log panels."""

    TITLE = "Curupira"
    CSS = """
    Screen {
        background: #000000;
        color: #e8e8e8;
    }
    #dashboard {
        height: 1fr;
        padding: 1;
    }
    Footer {
        background: #101010;
        color: #d0d0d0;
    }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "quit", "Sair", priority=True),
        Binding("f1", "show_help", "Ajuda"),
        Binding("f2", "toggle_pause", "Pausar"),
        Binding("f3", "show_config", "Config"),
        Binding("f5", "refresh_metrics", "Atualizar"),
    ]

    def __init__(
        self,
        settings: ApplicationSettings,
        gh: GhClient,
        version_control: VersionControl | None,
        telemetry: TaskTelemetry,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._gh = gh
        self._version_control = version_control
        self._telemetry = telemetry
        self._status = OrchestratorStatus()
        self._scheduler: TaskScheduler | None = None
        self._scheduler_task: asyncio.Task[None] | None = None
        self._log_handler: TuiLogHandler | None = None
        self._exit_code = 0
        self._paused = False

    @override
    def compose(self) -> ComposeResult:
        """Assemble the three dashboard panels and footer."""
        with Vertical(id="dashboard"):
            yield MetricsPanel(id="metrics-panel")
            yield AgentsPanel(id="agents-panel")
            yield LogsPanel(id="logs-panel")
        yield Footer()

    async def on_mount(self) -> None:
        """Wire logging, start the scheduler, and begin metric sampling."""
        log = self.query_one("#orchestrator-log", RichLog)
        self._log_handler = attach_rich_log(log)
        self._status.update((), self._settings.settings.max_active_tasks)
        self._refresh_agents()
        self._refresh_metrics()
        self.set_interval(1.0, self._on_tick)
        self._scheduler_task = asyncio.create_task(
            self._run_scheduler(), name="curupira-tui-scheduler"
        )

    async def on_unmount(self) -> None:
        """Detach the log handler and cancel the background scheduler."""
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler.close()
            self._log_handler = None
        if self._scheduler_task is not None and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            await asyncio.gather(self._scheduler_task, return_exceptions=True)

    async def _run_scheduler(self) -> None:
        """Drive the same continuous pipeline used by ``curu run --watch``."""
        settings = self._settings
        sessions = RunningSessionRepository(settings.settings.state_db_path)
        completed_tasks = CompletedTaskRepository(settings.settings.state_db_path)
        recovered = await sessions.list_all()
        cron = CronScheduleRepository(settings.settings.state_db_path)
        feeds = create_task_feeds(settings, self._gh, cron)
        initial_tasks = await poll_task_feeds(feeds)
        revalidate = GitHubTaskRevalidator(self._gh)
        executor = TaskExecutor(
            settings.settings,
            self._version_control,
            sessions,
            cron,
            telemetry=self._telemetry,
            pre_start_validator=revalidate,
        )
        scheduler = TaskScheduler(
            settings.settings,
            executor,
            on_active_tasks_changed=self._on_active_tasks_changed,
            task_validator=revalidate,
            completion_verifier=revalidate.completion_next_action,
            completed_tasks=completed_tasks,
        )
        self._scheduler = scheduler
        tasks = prioritize_task_stream(feeds, settings.settings.polling.poll_interval_seconds)
        try:
            await scheduler.run(tasks, resume_sessions=recovered, initial_tasks=initial_tasks)
            self._exit_code = 1 if scheduler.failed_tasks else 0
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).exception("Orchestrator scheduler failed")
            self._exit_code = 1
        finally:
            self.call_later(self.exit, self._exit_code)

    def _on_active_tasks_changed(self, tasks: Sequence[Task]) -> None:
        """Receive scheduler notifications on the asyncio thread and refresh widgets."""
        self._status.update(tuple(tasks), self._settings.settings.max_active_tasks)
        self.call_later(self._refresh_agents)
        self.call_later(self._refresh_activity)

    def _on_tick(self) -> None:
        """Periodic refresh for elapsed timers and host metrics."""
        self._refresh_metrics()
        self._refresh_agents()

    def _refresh_metrics(self) -> None:
        """Sample host CPU/memory and push values into the metrics panel."""
        panel = self.query_one("#metrics-panel", MetricsPanel)
        cpu = float(psutil.cpu_percent(interval=None))
        memory = psutil.virtual_memory()
        panel.update_host(cpu, int(memory.used), int(memory.total))
        self._refresh_activity()

    def _refresh_activity(self) -> None:
        """Update only the activity-limit portion of the metrics panel."""
        panel = self.query_one("#metrics-panel", MetricsPanel)
        panel.update_activity(len(self._status.tasks), self._status.limit)

    def _refresh_agents(self) -> None:
        """Redraw the running-agents list from the latest status snapshot."""
        self.query_one("#agents-panel", AgentsPanel).render_rows(self._status)

    def action_show_help(self) -> None:
        """Open the shortcuts help modal."""
        self.push_screen(HelpScreen())

    def action_show_config(self) -> None:
        """Open a modal with the loaded configuration summary."""
        settings = self._settings.settings
        automations = len(self._settings.coding_agents.automations)
        paused = "sim" if self._paused else "não"
        summary = (
            "[b]Configuração[/b]\n\n"
            f"Automações: {automations}\n"
            f"Máx. atividades: {settings.max_active_tasks}\n"
            f"Máx. pendentes: {settings.max_pending_tasks}\n"
            f"Workspace: {settings.workspace_dir}\n"
            f"Estado: {settings.state_db_path}\n"
            f"Pausado: {paused}\n\n"
            "Pressione Esc ou F3 para fechar."
        )
        self.push_screen(ConfigScreen(summary))

    def action_toggle_pause(self) -> None:
        """Pause or resume admission of new tasks in the scheduler."""
        if self._scheduler is None:
            return
        if self._scheduler.paused:
            self._scheduler.resume()
            self._paused = False
            logging.getLogger(__name__).info("Admissão de tarefas retomada")
        else:
            self._scheduler.pause()
            self._paused = True
            logging.getLogger(__name__).warning("Admissão de tarefas pausada")

    def action_refresh_metrics(self) -> None:
        """Force an immediate host-metrics refresh."""
        self._refresh_metrics()

    @override
    async def action_quit(self) -> None:
        """Exit the dashboard, cancelling the scheduler via unmount."""
        self.exit(self._exit_code)


async def run_orchestrator_tui(
    settings: ApplicationSettings,
    gh: GhClient,
    version_control: VersionControl | None,
    telemetry: TaskTelemetry,
) -> int:
    """Run the Textual orchestrator app and return its exit code."""
    app = OrchestratorApp(settings, gh, version_control, telemetry)
    result = await app.run_async()
    return 0 if result is None else result
