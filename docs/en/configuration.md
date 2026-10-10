# Configuration

## Generated model reference

The model reference below is generated from the public Pydantic models. Field types,
defaults, and constraints come directly from the source models; update those models rather
than maintaining a parallel field table here.

### Runtime settings

::: curupira.models.configuration.ExecutionSettings
    options:
      members:
        - max_active_tasks
        - max_pending_tasks
        - workspace_dir
        - state_db_path
        - otlp_endpoint
        - task_timeout_seconds
        - max_output_bytes
        - polling

::: curupira.models.configuration.PollingSettings
    options:
      show_root_heading: true

### Coding-agent defaults and automations

::: curupira.models.configuration.CodingAgentDefaults

::: curupira.models.configuration.CodingAgentsSettings

::: curupira.models.configuration.IssueAutomationConfiguration

::: curupira.models.configuration.PullRequestAutomationConfiguration

::: curupira.models.configuration.AzurePullRequestAutomationConfiguration

::: curupira.models.configuration.MondayAutomationConfiguration
::: curupira.models.configuration.TrelloAutomationConfiguration

::: curupira.models.configuration.CronAutomationConfiguration

### CLI profiles

::: curupira.models.profiles.OpenCodeCliProfile

::: curupira.models.profiles.CodexCliProfile

::: curupira.models.profiles.ClaudeCodeCliProfile

::: curupira.models.profiles.CursorCliProfile

The GitHub Copilot CLI profile is documented on its [provider page](providers/copilot.md#configuration-reference).

One TOML file contains global limits, coding-agent profiles, and automations. An automation watches issues, pull requests, or a cron schedule.

```toml
[settings]
max_active_tasks = 1
workspace_dir = "~/.curupira/workspaces"
state_db_path = "~/.curupira/state.sqlite3"

[settings.polling]
poll_interval_seconds = 30
batch_size = 100
cron_poll_interval_seconds = 1

[coding_agents.defaults]
profile = "opencode-default"
timezone = "UTC"

[coding_agents.profiles.opencode-default]
provider = "opencode"

[coding_agents.automations.resolve-ready-issues]
trigger_type = "issue"
repo = "acme/api"
query = "is:open label:agent-ready sort:created-asc"
prompt = "Resolve issue ${issue_number}: ${issue_title}\n\n${issue_body}"
```

Save this as `~/.curupira/settings.toml`. The keys under `profiles` and `automations` are user-chosen identifiers; `profile` connects an automation to an existing profile.

Each profile's `provider` selects a registered coding agent: `claude`, `codex`, `copilot`,
`cursor`, `gemini`, `opencode`, `pi`, or `qwen`, plus any provider added by an installed
[agent plugin](plugins.md#agent-plugins). `curu plugins list` shows every available
provider and its executable.

## Automations

`trigger_type` selects the source:

- `issue` discovers matching GitHub issues using GitHub Search syntax in `query`.
- `github-cli-pull-requests` discovers matching GitHub pull requests using `query`.
- `azure-cli-pull-requests` lists Azure DevOps pull requests through `az repos pr list`.
- `monday-cli-items` discovers items from a configured monday.com board through the official `mcli` CLI.
- `trello-cli-cards` discovers Trello cards through Scale-Flow's `trello-cli` for a configured `board_id`, optionally restricted to `list_ids`.
- `cron` produces occurrences from a five-field `schedule` instead of querying a forge.
- Installed [plugins](plugins.md) add their own trigger types; `curu plugins list` shows every available type and its prompt placeholders.

Each automation requires `repo` and `prompt`. GitHub triggers also require `query`; cron requires `schedule`. Azure DevOps automations use `repo` as `organization/project/repository` and optional `status` / branch filters instead of a search query. Trello automations require `board_id` and may set `list_ids` to select lists on that board; `repo` remains the repository Curupira checks out for coding tasks. Optional `profile` selects a CLI profile. Optional `path` pins the automation to an existing checkout or an alternative clone destination. Relative paths are resolved from the TOML file's directory. Different repositories cannot share one workspace path. Automations keep file order, and one-shot selection follows that order.

## monday.com source

Install monday.com's official [`mcli`](https://github.com/mondaycom/mcli) CLI with Go or Homebrew:

```sh
go install github.com/mondaycom/mcli/cmd/mcli@latest
# or: brew install mondaycom/tap/mcli
```

Authenticate once using the OS credential store, or set `MONDAY_API_TOKEN` in the environment:

```sh
mcli auth login --token <your-monday-api-token>
mcli auth status
```

Configure one automation with the board ID as a string. Curupira lists items as JSON, follows `mcli`'s cursor pagination, and deduplicates item IDs across pages and repeated polls.

```toml
[coding_agents.automations.monday-backlog]
trigger_type = "monday-cli-items"
repo = "acme/api"
board_id = "12345678901234567"
prompt = "Work on monday.com item ${monday_item_id}: ${monday_item_title} (board ${monday_board_id})."
```

The item ID and board ID are exposed as strings to preserve their full values. Use `mcli board list` to discover boards and `mcli item list --board <board-id>` to inspect a board directly. Curupira only reads items; board changes are outside this trigger's scope.

Placeholders use `${name}` syntax and are validated when the configuration loads. Common placeholders include `${repo}`, `${automation_id}`, `${task_type}`, `${task_number}`, `${task_title}`, `${task_body}`, and `${task_url}`. Issues and pull requests provide their respective number, title, body, and URL placeholders; pull requests also provide `${pull_request_is_draft}`, `${pull_request_head_ref}`, and `${pull_request_base_ref}`. Monday.com items provide `${monday_item_id}`, `${monday_item_title}`, and `${monday_board_id}`. For cron tasks, `${task_number}` is the occurrence timestamp.

## Checkout and setup

Each task uses its own worktree by default, created from the fetched remote default branch. Set `checkout = "main"` to use the shared checkout as-is; Curupira does not fetch, pull, or switch branches in that mode. `path` continues to select the base checkout.

`setup_script` is a repository-relative executable path with no absolute path or `..`. It runs directly only after a base checkout is freshly cloned, not for an existing checkout or in a task worktree. A nonzero exit prevents the agent from starting and removes the newly cloned checkout. Validation checks path syntax but does not require the script to exist. `run --dry-run` does not fetch, clone, create worktrees, or run setup.

## Scheduling and state

Polls fetch up to `batch_size` items (default 100, maximum 1000). Empty poll cycles back off from `poll_interval_seconds` (default 30 seconds) up to five minutes; discovery resets the wait. Automations deduplicate independently. Project queries keep the open state and filter board items to `Todo`.

`max_active_tasks` bounds concurrent agents. Checkouts using the same path run sequentially. Cron automations coalesce overdue ticks into one pending occurrence and never run themselves concurrently. `schedule` uses five cron fields; `timezone` is an IANA zone (default UTC), and optional `start_date`/`end_date` define an inclusive window. Without `start_date`, the window starts when the automation is first recorded.

State is stored in `state_db_path` (default `~/.curupira/state.sqlite3`), the dispatch lock in `~/.curupira/dispatch.lock`, and logs in `~/.curupira/logs`. An incompatible database causes an error rather than automatic deletion. Only one `run` or `tui` process may dispatch at a time.

## Telemetry

Set `settings.otlp_endpoint` to an OTLP/HTTP trace endpoint (for example, `http://localhost:4318/v1/traces`) to export one span per dispatched task. If omitted, no telemetry is exported.
