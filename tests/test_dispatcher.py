"""Source-independent dispatch, previews, and persisted execution snapshots."""

import logging
from pathlib import Path

import pytest

from curupira.config import ApplicationSettings
from curupira.dispatcher import dispatch_next_task
from curupira.executor import render_task_prompt
from curupira.models import (
    CommandRequest,
    GhIssue,
    GhPullRequest,
    ProcessResult,
    RunningCodingSession,
)
from curupira.runtime import create_execution_log_handler
from curupira.storage import CronScheduleRepository, RunningSessionRepository
from tests.fakes import AssigningAdapter, CallbackRunner, FakeGitHub, RecordingAdapter
from tests.helpers import issue_task


def settings(path: Path, trigger: str = "issue") -> ApplicationSettings:
    """Build a one-shot configuration with all state inside the test directory."""
    config: dict[str, object] = {
        "trigger_type": trigger,
        "repo": "acme/api",
        "path": path,
        "prompt": "Handle ${task_number}: ${task_title}",
    }
    if trigger == "cron":
        config.update(schedule="0 9 * * *", start_date="2020-01-01T00:00:00+00:00")
    else:
        config["query"] = "is:open"
    return ApplicationSettings.model_validate(
        {
            "settings": {"state_db_path": path / "state.sqlite3"},
            "coding_agents": {"automations": {"work": config}},
        }
    )


async def test_dispatch_renders_the_task_prompt_and_uses_shared_executor(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    configured = settings(tmp_path)
    gh = FakeGitHub(
        issues=[GhIssue(number=42, title="Fix", url="https://github.com/acme/api/issues/42")]
    )
    adapter = RecordingAdapter()
    outcome = await dispatch_next_task(
        configured, gh, adapter_factory=lambda _: adapter, version_control=gh.vcs
    )
    assert outcome.selected is not None
    assert outcome.selected.identity.automation_id == "work"
    assert outcome.process is not None
    assert outcome.process.returncode == 0
    assert adapter.requests[0].message == "Handle 42: Fix"
    assert gh.checkouts == [tmp_path]
    assert len(gh.worktrees) == 1
    assert gh.removed_worktrees == gh.worktrees
    assert adapter.requests[0].cwd == gh.worktrees[0]
    assert await RunningSessionRepository(configured.settings.state_db_path).list_all() == []
    assert "Starting task repo=acme/api type=issue id=42" in caplog.text
    assert "Completed task repo=acme/api type=issue id=42 result=success" in caplog.text


async def test_dispatch_logs_failed_task_with_identity_and_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO")
    configured = settings(tmp_path)
    gh = FakeGitHub(
        issues=[GhIssue(number=42, title="Fix", url="https://github.com/acme/api/issues/42")]
    )

    outcome = await dispatch_next_task(
        configured,
        gh,
        adapter_factory=lambda _: RecordingAdapter(returncode=7),
        version_control=gh.vcs,
    )

    assert outcome.process is not None
    assert outcome.process.returncode == 7
    assert "Failed task repo=acme/api type=issue id=42 result=failure" in caplog.text
    assert "process exited with status 7" in caplog.text


async def test_failed_dispatch_is_appended_to_the_central_log_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    root_logger.setLevel(logging.INFO)
    handler = create_execution_log_handler()
    root_logger.addHandler(handler)
    configured = settings(tmp_path)
    gh = FakeGitHub(
        issues=[GhIssue(number=42, title="Fix", url="https://github.com/acme/api/issues/42")]
    )

    try:
        await dispatch_next_task(
            configured,
            gh,
            adapter_factory=lambda _: RecordingAdapter(returncode=7),
            version_control=gh.vcs,
        )
    finally:
        root_logger.removeHandler(handler)
        handler.close()
        root_logger.setLevel(previous_level)

    log_path = tmp_path / ".curupira" / "logs" / "curupira.log"
    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "Starting task repo=acme/api type=issue id=42" in lines[0]
    assert "Failed task repo=acme/api type=issue id=42 result=failure" in lines[1]
    assert "process exited with status 7" in lines[1]
    assert lines[0][:4].isdigit()


@pytest.mark.parametrize("trigger", ["issue", "cron"])
async def test_dry_run_has_no_state_checkout_or_process_side_effects(
    tmp_path: Path, trigger: str
) -> None:
    configured = settings(tmp_path, trigger)
    gh = FakeGitHub(
        issues=[GhIssue(number=42, title="Fix", url="https://github.com/acme/api/issues/42")]
    )
    adapter = RecordingAdapter()
    outcome = await dispatch_next_task(
        configured, gh, dry_run=True, adapter_factory=lambda _: adapter, version_control=gh.vcs
    )
    assert outcome.selected is not None
    assert outcome.process is None
    assert not gh.checkouts
    assert not adapter.requests
    assert list(tmp_path.iterdir()) == []


async def test_empty_dispatch_does_not_create_state(tmp_path: Path) -> None:
    assert (await dispatch_next_task(settings(tmp_path), FakeGitHub())).selected is None
    assert list(tmp_path.iterdir()) == []


async def test_resume_discards_stale_issue_snapshot_and_uses_current_prompt(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path)
    original = issue_task(tmp_path, name="work")
    session = RunningCodingSession(
        task=original, session_id="original", message="Original task prompt"
    )
    await RunningSessionRepository(configured.settings.state_db_path).save(session)
    adapter = RecordingAdapter()
    gh = FakeGitHub(
        issues=[GhIssue(number=42, title="Changed", url="https://github.com/acme/api/issues/42")]
    )
    result = await dispatch_next_task(
        configured, gh, adapter_factory=lambda _: adapter, version_control=gh.vcs
    )
    assert result.selected is not None
    assert result.selected.title == "Changed"
    assert adapter.requests[0].session_id is None
    assert adapter.requests[0].message == "Handle 42: Changed"
    assert await RunningSessionRepository(configured.settings.state_db_path).list_all() == []


async def test_assigned_session_is_persisted_before_the_process_runs(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    repository = RunningSessionRepository(configured.settings.state_db_path)
    persisted_during_run: list[RunningCodingSession] = []

    async def snapshot() -> None:
        persisted_during_run.extend(await repository.list_all())

    adapter = AssigningAdapter(CallbackRunner(snapshot))
    gh = FakeGitHub(
        issues=[GhIssue(number=42, title="Fix", url="https://github.com/acme/api/issues/42")]
    )
    outcome = await dispatch_next_task(
        configured, gh, adapter_factory=lambda _: adapter, version_control=gh.vcs
    )
    assert outcome.selected is not None
    assert [session.session_id for session in persisted_during_run] == [
        adapter.requests[0].new_session_id
    ]
    assert persisted_during_run[0].task == outcome.selected
    assert await repository.list_all() == []


async def test_checkout_main_uses_shared_checkout_without_worktree(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    data = configured.model_dump()
    data["coding_agents"]["automations"]["work"]["checkout"] = "main"
    configured = ApplicationSettings.model_validate(data)
    gh = FakeGitHub(
        issues=[GhIssue(number=42, title="Fix", url="https://github.com/acme/api/issues/42")]
    )
    adapter = RecordingAdapter()

    await dispatch_next_task(
        configured, gh, adapter_factory=lambda _: adapter, version_control=gh.vcs
    )

    assert adapter.requests[0].cwd == tmp_path
    assert not gh.worktrees
    assert not gh.removed_worktrees


async def test_existing_checkout_does_not_rerun_setup(tmp_path: Path) -> None:
    configured = settings(tmp_path)
    data = configured.model_dump()
    data["coding_agents"]["automations"]["work"]["setup_script"] = "scripts/setup.sh"
    configured = ApplicationSettings.model_validate(data)
    gh = FakeGitHub(
        issues=[GhIssue(number=42, title="Fix", url="https://github.com/acme/api/issues/42")]
    )

    await dispatch_next_task(
        configured, gh, adapter_factory=lambda _: RecordingAdapter(), version_control=gh.vcs
    )

    assert not gh.setup_scripts


async def test_fresh_clone_setup_failure_removes_clone_and_skips_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailedSetupRunner:
        def __init__(self) -> None:
            self.requests: list[CommandRequest] = []

        async def run(self, request: CommandRequest) -> ProcessResult:
            self.requests.append(request)
            return ProcessResult(returncode=7, stderr="setup failed")

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    configured = settings(checkout)
    data = configured.model_dump()
    data["coding_agents"]["automations"]["work"]["setup_script"] = "setup.sh"
    configured = ApplicationSettings.model_validate(data)
    gh = FakeGitHub(
        issues=[GhIssue(number=42, title="Fix", url="https://github.com/acme/api/issues/42")]
    )
    runner = FailedSetupRunner()
    gh.vcs.cloned = True
    monkeypatch.setattr(gh.vcs, "_runner", runner)
    adapter = RecordingAdapter()

    outcome = await dispatch_next_task(
        configured, gh, adapter_factory=lambda _: adapter, version_control=gh.vcs
    )

    assert outcome.process is not None
    assert outcome.process.returncode == 7
    assert outcome.process.stderr == "setup failed"
    assert not adapter.requests
    assert not checkout.exists()
    assert runner.requests[0].executable == str(checkout / "setup.sh")


def test_common_placeholders_use_the_task_source(tmp_path: Path) -> None:
    assert render_task_prompt(issue_task(tmp_path)) == "Handle issue 42: Task 42"


async def test_one_shot_prioritizes_workflow_stage_over_automation_order(
    tmp_path: Path,
) -> None:
    data = settings(tmp_path).model_dump()
    data["coding_agents"]["automations"]["reviews"] = {
        "trigger_type": "github-cli-pull-requests",
        "repo": "acme/api",
        "query": "is:open",
        "prompt": "Review ${pull_request_head_ref} -> ${pull_request_base_ref}",
        "path": tmp_path,
    }
    configured = ApplicationSettings.model_validate(data)
    gh = FakeGitHub(
        issues=[GhIssue(number=42, title="Issue", url="https://github.com/acme/api/issues/42")],
        pulls=[
            GhPullRequest(
                number=12,
                title="Review",
                url="https://github.com/acme/api/pull/12",
                headRefName="feature",
                baseRefName="main",
            )
        ],
    )
    outcome = await dispatch_next_task(configured, gh, dry_run=True)
    assert outcome.selected is not None
    assert outcome.selected.identity.task_type == "github-cli-pull-requests"
    gh.issues = []
    adapter = RecordingAdapter()
    outcome = await dispatch_next_task(
        configured, gh, adapter_factory=lambda _: adapter, version_control=gh.vcs
    )
    assert outcome.selected is not None
    assert outcome.selected.identity.task_type == "github-cli-pull-requests"
    assert adapter.requests[0].message == "Review feature -> main"


async def test_cron_execution_completes_claimed_state_through_shared_executor(
    tmp_path: Path,
) -> None:
    configured = settings(tmp_path, "cron")
    gh = FakeGitHub()
    outcome = await dispatch_next_task(
        configured, gh, adapter_factory=lambda _: RecordingAdapter(), version_control=gh.vcs
    )
    assert outcome.selected is not None
    assert outcome.selected.identity.task_type == "cron"
    state = await CronScheduleRepository(configured.settings.state_db_path).state("work")
    assert state is not None
    assert state.last_execution_at is not None
    assert state.pending_scheduled_for is None
