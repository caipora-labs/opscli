"""Native argument translation, GitHub/Azure boundaries, and optional provider options."""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from typing_extensions import override

from curupira.agents import create_cli_adapter
from curupira.agents.base import CodingAgentCliAdapter
from curupira.clients.az import AzClient, organization_url
from curupira.clients.gh import GhClient
from curupira.clients.process import AsyncProcessRunner
from curupira.errors import (
    CliExecutionError,
    CliOutputError,
    UnsupportedCodingAgentError,
)
from curupira.models import (
    AzPullRequestSearchRequest,
    ClaudeCodeCliProfile,
    CliProfile,
    CodexCliProfile,
    CodingTaskRequest,
    CommandRequest,
    CursorCliProfile,
    GhIssueSearchRequest,
    GhPullRequestSearchRequest,
    GhTaskViewRequest,
    ProcessResult,
)
from curupira.vcs.github_cli import GitHubCliVersionControl


class RecordingRunner(AsyncProcessRunner):
    """Simulate native stdout events and record literal argument vectors."""

    def __init__(self, *results: ProcessResult) -> None:
        self.results = list(results)
        self.requests: list[CommandRequest] = []

    @override
    async def run(
        self,
        request: CommandRequest,
        *,
        on_stdout_line: Callable[[str], Awaitable[None]] | None = None,
    ) -> ProcessResult:
        self.requests.append(request)
        result = self.results.pop(0)
        if on_stdout_line is not None:
            for line in result.stdout.splitlines():
                await on_stdout_line(line)
        return result


@pytest.mark.parametrize("provider", ["opencode", "codex", "claude"])
def test_factory_selects_native_provider_adapter(provider: str) -> None:
    assert isinstance(create_cli_adapter(provider), CodingAgentCliAdapter)


def test_factory_rejects_unknown_provider() -> None:
    with pytest.raises(UnsupportedCodingAgentError):
        create_cli_adapter("unknown")


@pytest.mark.parametrize("profile", [CodexCliProfile(), ClaudeCodeCliProfile(), CursorCliProfile()])
async def test_omitted_options_and_option_like_prompts_are_literal(
    tmp_path: Path, profile: CliProfile
) -> None:
    runner = RecordingRunner(ProcessResult(returncode=0))
    await create_cli_adapter(profile.provider, runner).run_task(
        CodingTaskRequest(cwd=tmp_path, profile=profile, message="--help; this is task data")
    )
    arguments = runner.requests[0].arguments
    assert arguments[-2:] == ("--", "--help; this is task data")
    assert not {
        "--agent",
        "--model",
        "--effort",
        "--variant",
        "--profile",
        "--mode",
        "--force",
        "--trust",
        "--auto",
        "--permission-mode",
    }.intersection(arguments)
    assert runner.requests[0].cwd == tmp_path


@pytest.mark.parametrize(
    ("profile", "flag", "value"),
    [
        (
            ClaudeCodeCliProfile(agent="custom-reviewer", model="sonnet", effort="high"),
            "--agent",
            "custom-reviewer",
        ),
        (CodexCliProfile(agent="work"), "--profile", "work"),
    ],
)
async def test_agent_option_uses_its_provider_native_flag(
    tmp_path: Path, profile: CliProfile, flag: str, value: str
) -> None:
    runner = RecordingRunner(ProcessResult(returncode=0))
    await create_cli_adapter(profile.provider, runner).run_task(
        CodingTaskRequest(cwd=tmp_path, profile=profile, message="Review")
    )
    arguments = runner.requests[0].arguments
    assert arguments[arguments.index(flag) + 1] == value


def test_provider_profiles_match_current_cli_argument_contracts(tmp_path: Path) -> None:
    requests = [
        (
            CodexCliProfile(
                model="gpt-5.4",
                agent="work",
                effort="high",
                sandbox="workspace-write",
            ),
            "codex",
            (
                "exec",
                "resume",
                "native-session",
                "--model",
                "gpt-5.4",
                "--profile",
                "work",
                "--config",
                'model_reasoning_effort="high"',
                "--sandbox",
                "workspace-write",
                "--json",
                "--",
                "Handle task",
            ),
        ),
        (
            CursorCliProfile(model="composer-2.5", agent="plan", force=True, trust=True),
            "cursor",
            (
                "--print",
                "--output-format",
                "stream-json",
                "--resume",
                "native-session",
                "--mode",
                "plan",
                "--model",
                "composer-2.5",
                "--force",
                "--trust",
                "--",
                "Handle task",
            ),
        ),
    ]
    for profile, provider, expected in requests:
        arguments = create_cli_adapter(provider).build_arguments(
            CodingTaskRequest(
                cwd=tmp_path, profile=profile, session_id="native-session", message="Handle task"
            )
        )
        assert arguments == expected


@pytest.mark.parametrize(
    ("profile", "event"),
    [
        (
            CodexCliProfile(),
            '{"type":"thread.started","thread_id":"native"}\n{"type":"item.completed","item":{"type":"agent_message","text":"Done"}}',
        ),
        (CursorCliProfile(), '{"type":"result","session_id":"native","result":"Done"}'),
    ],
)
async def test_native_session_events_and_resume(
    tmp_path: Path, profile: CliProfile, event: str
) -> None:
    runner = RecordingRunner(
        ProcessResult(returncode=0, stdout="not JSON\n" + event + "\n" + event)
    )
    reported: list[str] = []

    async def callback(session_id: str) -> None:
        reported.append(session_id)

    result = await create_cli_adapter(profile.provider, runner).run_task(
        CodingTaskRequest(cwd=tmp_path, message="Continue", profile=profile, session_id="native"),
        on_session_started=callback,
    )
    assert reported == ["native"]
    assert "Done" in result.stdout
    assert "native" in runner.requests[0].arguments


def test_explicit_permission_options_are_provider_native(tmp_path: Path) -> None:
    profiles: list[CliProfile] = [
        CursorCliProfile(force=True, trust=True),
        CodexCliProfile(sandbox="workspace-write", auto_review=True, effort="high"),
    ]
    expected = ["--force", "--sandbox"]
    for profile, flag in zip(profiles, expected, strict=True):
        arguments = create_cli_adapter(profile.provider).build_arguments(
            CodingTaskRequest(cwd=tmp_path, message="Work", profile=profile)
        )
        assert flag in arguments


async def test_github_query_is_not_shell_interpreted_and_json_is_validated() -> None:
    runner = RecordingRunner(
        ProcessResult(
            returncode=0,
            stdout='[{"number":42,"title":"Work","url":"https://github.com/acme/api/issues/42"}]',
        )
    )
    issues = await GhClient(runner).list_issues(
        GhIssueSearchRequest(repo="acme/api", query='label:"ready"; literal data')
    )
    assert issues[0].number == 42
    assert 'label:"ready"; literal data' in runner.requests[0].arguments


async def test_project_query_accepts_newline_json_and_requests_board_filter() -> None:
    output = "\n".join(
        json.dumps(
            {"number": number, "title": "Work", "url": "https://github.com/acme/api/issues/1"}
        )
        for number in (1, 2)
    )
    runner = RecordingRunner(ProcessResult(returncode=0, stdout=output))
    assert (
        len(
            await GhClient(runner).list_issues(
                GhIssueSearchRequest(repo="acme/api", query="is:open project:acme/9")
            )
        )
        == 2
    )
    arguments = runner.requests[0].arguments
    assert arguments[arguments.index("--state") + 1] == "open"
    assert "is:open project:acme/9" in arguments
    assert "--jq" in arguments
    assert arguments[arguments.index("--jq") + 1] == (
        '.[] | select(any(.projectItems[]?; .status.name == "Todo"))'
    )


async def test_pull_request_branch_metadata_is_preserved() -> None:
    runner = RecordingRunner(
        ProcessResult(
            returncode=0,
            stdout='[{"number":12,"title":"Review","url":"https://github.com/acme/api/pull/12","isDraft":true,"headRefName":"feature","baseRefName":"main"}]',
        )
    )
    pulls = await GhClient(runner).list_pull_requests(
        GhPullRequestSearchRequest(repo="acme/api", query="is:open")
    )
    assert pulls[0].is_draft
    assert pulls[0].head_ref_name == "feature"


async def test_pull_request_search_includes_head_checks_and_closing_issue_metadata() -> None:
    runner = RecordingRunner(
        ProcessResult(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "number": 12,
                        "title": "Review",
                        "url": "https://github.com/acme/api/pull/12",
                        "state": "OPEN",
                        "isDraft": False,
                        "headRefOid": "abc123",
                        "mergeable": "MERGEABLE",
                        "mergeStateStatus": "CLEAN",
                        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
                        "closingIssuesReferences": [
                            {"number": 42, "repository": {"nameWithOwner": "acme/api"}}
                        ],
                    }
                ]
            ),
        )
    )

    pull = (
        await GhClient(runner).list_pull_requests(
            GhPullRequestSearchRequest(repo="acme/api", query="is:open")
        )
    )[0]

    assert pull.head_ref_oid == "abc123"
    assert pull.merge_state_status == "CLEAN"
    assert pull.status_check_rollup[0].conclusion == "SUCCESS"
    assert pull.closing_issues_references[0].number == 42
    requested_fields = runner.requests[0].arguments[
        runner.requests[0].arguments.index("--json") + 1
    ]
    assert "headRefOid" in requested_fields
    assert "closingIssuesReferences" in requested_fields


async def test_github_view_commands_fetch_current_issue_and_pull_request_state() -> None:
    runner = RecordingRunner(
        ProcessResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "number": 42,
                    "title": "Issue",
                    "url": "https://github.com/acme/api/issues/42",
                    "state": "OPEN",
                }
            ),
        ),
        ProcessResult(
            returncode=0,
            stdout=json.dumps(
                {
                    "number": 12,
                    "title": "Pull",
                    "url": "https://github.com/acme/api/pull/12",
                    "state": "CLOSED",
                    "headRefOid": "abc123",
                    "mergedAt": None,
                }
            ),
        ),
    )
    client = GhClient(runner)
    request = GhTaskViewRequest(repo="acme/api", number=42)

    issue = await client.view_issue(request)
    pull = await client.view_pull_request(request.model_copy(update={"number": 12}))

    assert issue.state == "OPEN"
    assert pull.state == "CLOSED"
    assert pull.head_ref_oid == "abc123"
    assert runner.requests[0].arguments[:5] == ("issue", "view", "42", "--repo", "acme/api")
    assert runner.requests[1].arguments[:5] == ("pr", "view", "12", "--repo", "acme/api")


async def test_pull_request_jq_filter_is_passed_to_gh() -> None:
    runner = RecordingRunner(
        ProcessResult(
            returncode=0,
            stdout='[{"number":12,"title":"Review","url":"https://github.com/acme/api/pull/12"}]',
        )
    )
    await GhClient(runner).list_pull_requests(
        GhPullRequestSearchRequest(
            repo="acme/api", query="is:open", jq='.[] | select(.mergeable == "MERGEABLE")'
        )
    )

    arguments = runner.requests[0].arguments
    assert arguments[arguments.index("--jq") + 1] == '.[] | select(.mergeable == "MERGEABLE")'


@pytest.mark.parametrize("output", ["not JSON", "42", '[{"number":0}]'])
async def test_invalid_github_payloads_fail(output: str) -> None:
    with pytest.raises(CliOutputError):
        await GhClient(RecordingRunner(ProcessResult(returncode=0, stdout=output))).list_issues(
            GhIssueSearchRequest(repo="acme/api", query="is:open")
        )


async def test_nontransient_github_errors_are_not_retried() -> None:
    runner = RecordingRunner(ProcessResult(returncode=1, stderr="not authenticated"))
    with pytest.raises(CliExecutionError):
        await GhClient(runner).list_issues(GhIssueSearchRequest(repo="acme/api", query="is:open"))
    assert len(runner.requests) == 1


async def test_transient_github_errors_are_retried() -> None:
    runner = RecordingRunner(
        ProcessResult(returncode=1, stderr="HTTP 503"), ProcessResult(returncode=0, stdout="[]")
    )
    assert (
        await GhClient(runner).list_issues(GhIssueSearchRequest(repo="acme/api", query="is:open"))
        == []
    )
    assert len(runner.requests) == 2


async def test_github_cli_version_control_clones_with_literal_repo_and_destination(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(ProcessResult(returncode=0))
    await GitHubCliVersionControl(runner).clone("acme/api", tmp_path / "checkout")

    assert runner.requests[0].executable == "gh"
    assert runner.requests[0].arguments == ("repo", "clone", "acme/api", str(tmp_path / "checkout"))


async def test_github_cli_version_control_reports_clone_failure() -> None:
    runner = RecordingRunner(ProcessResult(returncode=1, stderr="not authenticated"))
    with pytest.raises(CliExecutionError):
        await GitHubCliVersionControl(runner).clone("acme/api", Path("checkout"))


def test_organization_url_normalizes_names_and_preserves_urls() -> None:
    assert organization_url("contoso") == "https://dev.azure.com/contoso"
    assert organization_url("https://dev.azure.com/contoso/") == "https://dev.azure.com/contoso"


async def test_azure_pull_request_list_uses_literal_arguments_and_validates_json() -> None:
    runner = RecordingRunner(
        ProcessResult(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "pullRequestId": 12,
                        "title": "Review",
                        "description": "Body",
                        "isDraft": True,
                        "sourceRefName": "refs/heads/feature",
                        "targetRefName": "refs/heads/main",
                        "_links": {
                            "web": {
                                "href": (
                                    "https://dev.azure.com/contoso/api-project/_git/api"
                                    "/pullrequest/12"
                                )
                            }
                        },
                    }
                ]
            ),
        )
    )

    pulls = await AzClient(runner).list_pull_requests(
        AzPullRequestSearchRequest(
            organization="contoso",
            project="api-project",
            repository="api",
            limit=25,
            status="active",
            source_branch="feature",
            target_branch="main",
        )
    )

    assert pulls[0].pull_request_id == 12
    assert pulls[0].is_draft is True
    assert pulls[0].web_url() == (
        "https://dev.azure.com/contoso/api-project/_git/api/pullrequest/12"
    )
    assert runner.requests[0].executable == "az"
    assert runner.requests[0].arguments == (
        "repos",
        "pr",
        "list",
        "--organization",
        "https://dev.azure.com/contoso",
        "--project",
        "api-project",
        "--repository",
        "api",
        "--status",
        "active",
        "--top",
        "25",
        "--include-links",
        "--output",
        "json",
        "--source-branch",
        "feature",
        "--target-branch",
        "main",
    )


@pytest.mark.parametrize("output", ["not JSON", "{}", '[{"pullRequestId":0}]'])
async def test_invalid_azure_payloads_fail(output: str) -> None:
    client = AzClient(RecordingRunner(ProcessResult(returncode=0, stdout=output)))
    with pytest.raises(CliOutputError):
        await client.list_pull_requests(
            AzPullRequestSearchRequest(
                organization="contoso", project="api-project", repository="api"
            )
        )


async def test_nontransient_azure_errors_are_not_retried() -> None:
    runner = RecordingRunner(ProcessResult(returncode=1, stderr="not authenticated"))
    with pytest.raises(CliExecutionError):
        await AzClient(runner).list_pull_requests(
            AzPullRequestSearchRequest(
                organization="contoso", project="api-project", repository="api"
            )
        )
    assert len(runner.requests) == 1


async def test_transient_azure_errors_are_retried() -> None:
    runner = RecordingRunner(
        ProcessResult(returncode=1, stderr="HTTP 503"), ProcessResult(returncode=0, stdout="[]")
    )
    assert (
        await AzClient(runner).list_pull_requests(
            AzPullRequestSearchRequest(
                organization="contoso", project="api-project", repository="api"
            )
        )
        == []
    )
    assert len(runner.requests) == 2
