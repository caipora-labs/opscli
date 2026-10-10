"""CLI validation, argument contracts, and user-facing failure statuses."""

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from curupira.cli import (
    CliOptions,
    _batch_stream,
    _limit_initial_tasks,
    _program_name,
    app,
    async_main,
    main,
)
from curupira.config import load_settings
from curupira.models import IssueAutomationConfiguration, Task
from curupira.runtime import DispatchInstanceLock, dispatch_home
from curupira.tasks.base import TaskFeed
from tests.helpers import issue_task

runner = CliRunner()


class SequenceFeed(TaskFeed):
    """Return finite batches in order for batch-mode tests."""

    def __init__(self, batches: list[list[Task]]) -> None:
        self.batches = batches

    async def poll(self, *, preview: bool = False) -> list[Task]:
        return self.batches.pop(0) if self.batches else []

    async def stream(self) -> AsyncIterator[Task]:
        for batch in self.batches:
            for task in batch:
                yield task


def test_program_name_follows_the_invoked_console_script(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _program_name("curupira") == "curupira"
    assert _program_name("/usr/local/bin/curu") == "curu"
    assert _program_name("/usr/local/bin/curu.EXE") == "curu"
    assert _program_name("/usr/bin/pytest") == "curupira"

    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/curu"])
    help_text = runner.invoke(app, ["--help"], prog_name=_program_name()).output
    assert "Usage: curu" in help_text
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/curupira"])
    help_text = runner.invoke(app, ["--help"], prog_name=_program_name()).output
    assert "Usage: curupira" in help_text


def test_cli_supports_source_independent_commands(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--config", str(tmp_path / "config.toml"), "run", "--dry-run"])
    # dry-run still loads config; missing file exits 2, but the command is accepted
    assert "No such option" not in result.output
    assert result.exit_code in {0, 1, 2}

    run_help = runner.invoke(app, ["run", "--help"]).output
    assert "--watch" in run_help
    assert "--size" in run_help
    assert "tui" in runner.invoke(app, ["--help"]).output
    assert runner.invoke(app, ["watch", "--help"]).exit_code != 0
    assert runner.invoke(app, ["batch", "--help"]).exit_code != 0


@pytest.mark.parametrize("value", ["0", "-1", "nope"])
def test_run_size_must_be_a_positive_integer(value: str) -> None:
    result = runner.invoke(app, ["run", "--size", value])
    assert result.exit_code != 0


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["run", "--watch", "--dry-run"], "--dry-run"),
        (["run", "--watch", "--size", "2"], "--size"),
        (["run", "--dry-run", "--size", "2"], "--size"),
    ],
)
def test_run_rejects_incompatible_option_combinations(args: list[str], needle: str) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code != 0
    assert needle in result.output


def test_main_accepts_tui_subcommand_in_help() -> None:
    result = runner.invoke(app, ["tui", "--help"])
    assert result.exit_code == 0
    assert "interactive terminal dashboard" in result.output.lower()


def test_main_version_flag() -> None:
    result = runner.invoke(app, ["--version"], prog_name="curu")
    assert result.exit_code == 0
    assert "curu " in result.output


async def test_batch_stream_drains_each_feed_until_empty(tmp_path: Path) -> None:
    first, second = issue_task(tmp_path, 1), issue_task(tmp_path, 2)
    feed = SequenceFeed([[first], [second]])

    assert [task async for task in _batch_stream([feed], None)] == [first, second]


async def test_batch_stream_stops_at_size_even_when_more_tasks_exist(tmp_path: Path) -> None:
    tasks = [issue_task(tmp_path, number) for number in range(1, 4)]
    feed = SequenceFeed([tasks])

    assert [task async for task in _batch_stream([feed], 2)] == tasks[:2]


async def test_initial_batch_counts_against_finite_size_limit(tmp_path: Path) -> None:
    tasks = [issue_task(tmp_path, number) for number in range(1, 4)]

    admitted, remaining = _limit_initial_tasks(tasks, 1)

    assert admitted == tasks[:1]
    assert remaining == 0


async def test_batch_stream_exits_cleanly_for_empty_queue() -> None:
    assert [task async for task in _batch_stream([SequenceFeed([])], None)] == []


def test_parser_defaults_to_central_settings_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    options = CliOptions(command="validate", config=tmp_path / ".curupira" / "settings.toml")
    assert options.config == tmp_path / ".curupira" / "settings.toml"
    # Ensure the Typer default resolves the same path when HOME is patched.
    from curupira.runtime import default_config_path

    assert default_config_path() == tmp_path / ".curupira" / "settings.toml"


async def test_default_configuration_is_loaded_from_user_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config = tmp_path / ".curupira" / "settings.toml"
    config.parent.mkdir()
    config.write_text(
        '[coding_agents.automations.daily]\ntrigger_type="cron"\nrepo="acme/api"\n'
        'schedule="0 9 * * *"\nprompt="Maintain ${repo}"\n',
        encoding="utf-8",
    )
    options = CliOptions(command="validate", config=config)

    assert await async_main(options) == 0
    assert "Configuration is valid (1 automations" in capsys.readouterr().out


async def test_dispatch_refuses_to_run_when_another_instance_holds_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    lock = DispatchInstanceLock(dispatch_home() / "dispatch.lock")
    lock.acquire()
    try:
        options = CliOptions(command="run", config=tmp_path / "missing.toml")

        assert await async_main(options) == 1
        assert "another curupira process is already running" in capsys.readouterr().err
    finally:
        lock.release()


async def test_validate_is_side_effect_free_for_cron_only_configuration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[settings]\nstate_db_path="state.sqlite3"\n'
        '[coding_agents.automations.daily]\ntrigger_type="cron"\nrepo="acme/api"\n'
        'schedule="0 9 * * *"\nprompt="Maintain ${repo}"\n',
        encoding="utf-8",
    )
    assert await async_main(CliOptions(command="validate", config=path)) == 0
    assert "1 automations" in capsys.readouterr().out
    assert sorted(item.name for item in tmp_path.iterdir()) == ["config.toml"]


async def test_example_configuration_is_valid(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parents[1] / "curupira.example.toml"
    settings = await load_settings(example)
    assert sorted(settings.resolve_automations()) == [
        "resolve-ready-issues",
        "review-azure-pull-requests",
        "review-pull-requests",
        "weekly-maintenance",
    ]
    issue_automation = settings.coding_agents.automations["resolve-ready-issues"]
    assert isinstance(issue_automation, IssueAutomationConfiguration)
    assert issue_automation.query == "is:open label:agent-ready -linked:pr sort:created-asc"
    assert list(tmp_path.iterdir()) == []


async def test_configuration_error_has_actionable_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await async_main(CliOptions(command="run", config=tmp_path / "absent.toml")) == 2
    assert "configuration file not found" in capsys.readouterr().err


def test_main_returns_typer_exit_codes(tmp_path: Path) -> None:
    assert main(["--config", str(tmp_path / "absent.toml"), "validate"]) == 2
