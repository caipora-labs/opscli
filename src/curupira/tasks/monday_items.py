"""monday.com board item discovery through the official ``mcli`` CLI."""

import json
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from typing_extensions import override

from curupira.clients.process import AsyncProcessRunner
from curupira.errors import (
    CliExecutionError,
    CliLaunchError,
    CliNotFoundError,
    CliTimeoutError,
    DispatchError,
)
from curupira.models import (
    CommandRequest,
    MondayAutomationConfiguration,
    ProcessResult,
    ResolvedAutomation,
    Task,
    TaskIdentity,
)
from curupira.tasks.base import FeedDependencies, TaskFeed, TaskSource, Trigger
from curupira.tasks.feed import PollingTaskFeed
from curupira.tasks.registry import register

MAX_MCLI_PAGE_SIZE = 500
MONDAY_WEB_URL = "https://monday.com"
ItemIdentifier = Annotated[str, Field(min_length=1)]


class MondayItemPayload(BaseModel):
    """The item fields required from mcli's structured list response."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: ItemIdentifier
    name: str

    @field_validator("id", mode="before")
    @classmethod
    def preserve_identifier(cls, value: object) -> object:
        """Accept numeric JSON IDs while normalizing them to stable strings."""
        return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


class MondayItemPage(BaseModel):
    """One JSON page from ``mcli item list``."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    items: list[MondayItemPayload]
    cursor: str = ""


class MondayItemSource(TaskSource):
    """Discover monday.com board items through subprocess calls to mcli."""

    def __init__(self, runner: AsyncProcessRunner) -> None:
        self._runner = runner
        self._cursor = ""

    @override
    async def discover(self, automation: ResolvedAutomation, limit: int) -> list[Task]:
        """Fetch up to ``limit`` unique board items, following mcli page cursors."""
        config = automation.configuration
        if not isinstance(config, MondayAutomationConfiguration):
            raise ValueError("monday.com source requires a monday-cli-items configuration")
        if limit < 1:
            return []

        tasks: list[Task] = []
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        cursor = self._cursor
        while len(tasks) < limit:
            page_limit = min(limit - len(tasks), MAX_MCLI_PAGE_SIZE)
            arguments = [
                "item",
                "list",
                "--board",
                config.board_id,
                "--limit",
                str(page_limit),
                "--json",
            ]
            if cursor:
                arguments.extend(("--cursor", cursor))
            result = await self._run(tuple(arguments))
            page = _parse_page(result)
            if not page.cursor or page.cursor == cursor or page.cursor in seen_cursors:
                self._cursor = ""
            else:
                self._cursor = page.cursor
                seen_cursors.add(page.cursor)
            for item in page.items:
                if item.id in seen_ids:
                    continue
                seen_ids.add(item.id)
                tasks.append(
                    Task(
                        identity=TaskIdentity(
                            automation_id=automation.automation_id,
                            repo=config.repo,
                            task_type=config.trigger_type,
                            id=item.id,
                        ),
                        automation=automation,
                        title=item.name,
                        url=f"{MONDAY_WEB_URL}/boards/{config.board_id}/pulses/{item.id}",
                        attributes={"monday_board_id": config.board_id},
                    )
                )
                if len(tasks) >= limit:
                    return tasks
            if not self._cursor:
                break
            cursor = self._cursor
        return tasks

    async def _run(self, arguments: tuple[str, ...]) -> ProcessResult:
        try:
            result = await self._runner.run(
                CommandRequest(
                    executable="mcli",
                    arguments=arguments,
                    timeout=60.0,
                    max_output_bytes=10_000_000,
                )
            )
        except CliNotFoundError as error:
            raise DispatchError(
                "mcli is not installed; install it with `go install "
                "github.com/mondaycom/mcli/cmd/mcli@latest`"
            ) from error
        except CliLaunchError as error:
            raise DispatchError(f"could not start mcli: {error}") from error
        except CliTimeoutError as error:
            raise DispatchError(f"mcli item list timed out: {error}") from error
        except CliExecutionError as error:
            detail = error.stderr.strip()
            hint = (
                " Check `mcli auth status` and authenticate with `mcli auth login`."
                if _looks_like_authentication_error(detail)
                else ""
            )
            raise DispatchError(
                f"mcli item list failed (exit {error.returncode}). {detail}{hint}"
            ) from error
        if result.returncode != 0:
            detail = result.stderr.strip() or "no error details were reported"
            hint = (
                " Check `mcli auth status` and authenticate with `mcli auth login`."
                if _looks_like_authentication_error(detail)
                else ""
            )
            raise DispatchError(
                f"mcli item list failed (exit {result.returncode}): {detail}.{hint}"
            )
        return result


class MondayItemTrigger(Trigger):
    """Trigger implementation for monday.com item automations."""

    trigger_type = "monday-cli-items"
    configuration_model = MondayAutomationConfiguration

    @classmethod
    @override
    def prompt_fields(cls) -> frozenset[str]:
        """Return monday.com item-specific prompt placeholders."""
        return frozenset({"monday_item_id", "monday_item_title", "monday_board_id"})

    @override
    def prompt_context(self, task: Task) -> dict[str, str]:
        """Map a monday.com item task into prompt placeholders."""
        return {
            "monday_item_id": task.identity.id,
            "monday_item_title": task.title,
            "monday_board_id": task.attributes["monday_board_id"],
        }

    @override
    def build_feed(
        self, automation: ResolvedAutomation, dependencies: FeedDependencies
    ) -> TaskFeed:
        """Build a deduplicating polling feed backed by mcli."""
        return PollingTaskFeed(
            automation, dependencies.polling, MondayItemSource(dependencies.runner)
        )


def _parse_page(result: ProcessResult) -> MondayItemPage:
    if result.output_truncated:
        raise DispatchError("mcli item list output exceeded Curupira's capture limit")
    try:
        return MondayItemPage.model_validate_json(result.stdout)
    except (ValidationError, ValueError, json.JSONDecodeError) as error:
        raise DispatchError(f"mcli item list returned unexpected JSON: {error}") from error


def _looks_like_authentication_error(message: str) -> bool:
    lowered = message.lower()
    return any(word in lowered for word in ("auth", "token", "unauthorized", "unauthenticated"))


register(MondayItemTrigger())
