# AGENTS.md

Instructions for coding agents working in this repository. Humans should start with
[CONTRIBUTING.md](CONTRIBUTING.md); this file is the short operating manual both share.
Explicit instructions in a task or prompt override this file.

## Project

Curupira dispatches GitHub, Azure DevOps, Trello, monday.com, and cron tasks to local coding-agent CLIs
(OpenCode, Codex, Claude Code, Cursor, Gemini CLI, GitHub Copilot CLI, Kilo CLI, pi, and
Qwen Code). The package, import, and main console script are `curupira`; `curu` is the
short alias.

Stack: Python 3.11+ (pure Python, `src/` layout), [uv](https://docs.astral.sh/uv/),
Pydantic v2 and pydantic-settings (TOML configuration), Typer (CLI), Textual (TUI),
asyncio subprocesses, SQLite, OpenTelemetry, pytest, Ruff, Pyrefly (strict), and
[prek](https://prek.j178.dev/) for Git hooks.

## Commands

Run everything from the repository root. CI runs the same commands and every one must pass.

```bash
uv sync --dev                                              # install the project and dev tools
uv run --no-sync prek install                              # Git pre-commit hooks (prek)
uv run --no-sync prek run --all-files                      # run all hooks on demand
uv run --no-sync pytest                                    # full test suite
uv run --no-sync pytest tests/test_cli.py -k example      # one file or test
uv run --no-sync pytest --cov                              # branch coverage, must stay >= 85%
uv run --no-sync ruff check .                              # lint (add --fix for safe fixes)
uv run --no-sync ruff format --check .                     # formatting
uv run --no-sync pyrefly check                             # strict type check
uv run --no-sync curupira --config curupira.example.toml validate
uv build && uv run --no-sync twine check dist/*            # packaging
```

Git hooks are managed by [`prek`](https://prek.j178.dev/) via `.pre-commit-config.yaml`.
Ruff and Pyrefly hooks invoke `uv run --no-sync` so they match the locked project
environment (do not pin a separate Ruff wheel in the hook config).

Documentation: `uv sync --group docs`, then `uv run mkdocs build --strict`.

## Repository map

Each layer owns its contract in `base.py`; implementations depend on the contract, never
on each other.

| Path | Responsibility |
| --- | --- |
| `src/curupira/models/` | Pydantic contracts: configuration, CLI profiles, tasks, CLI payloads. |
| `src/curupira/config.py` | Loads and resolves the TOML configuration (`ApplicationSettings`). |
| `src/curupira/tasks/` | Task discovery: `Trigger`, `TaskSource`, `TaskFeed`; one module per trigger, registered in `tasks/registry.py`. Each trigger owns its `configuration_model` and lifecycle hooks. |
| `src/curupira/plugins.py` | Stable plugin API and `curupira.triggers`/`curupira.agents` entry-point discovery; plugins import only this module. |
| `src/curupira/vcs/` | Repository checkout and worktrees: `VersionControl`. |
| `src/curupira/agents/` | Coding-agent CLI adapters: `CodingAgentCliAdapter`, built by `create_cli_adapter`; one module per provider, registered in `agents/registry.py`. Each adapter owns its `profile_model`. |
| `src/curupira/clients/` | `gh`/`az` wrappers and `AsyncProcessRunner`, the only place that starts processes. |
| `src/curupira/storage/` | SQLite persistence for sessions and cron state. |
| `src/curupira/cli.py`, `tui/` | Typer commands and the Textual dashboard. |
| `tests/` | Mirrors `src/`; shared fakes in `tests/fakes.py`, builders in `tests/helpers.py`. |
| `docs/en/` | Canonical documentation; `docs/pt/` and `docs/es/` are translations. One page per provider in `docs/en/providers/`. |
| `main.py` | MkDocs macros; the provider table and install list come from the agent registry. |

## Code style

Follow the Zen of Python (`python -m this`): explicit, flat, and simple beats clever.

- Every function and method is fully typed; Pyrefly runs in strict mode. Avoid `Any`
  (test builders may accept `**overrides: Any`).
- Data crossing a boundary (TOML, CLI JSON, SQLite rows) is a Pydantic model, never a
  loose `dict`. Configuration models extend `ValidatedModel` (frozen, `extra="forbid"`);
  external CLI payloads are frozen models that ignore unknown fields.
- Express a validation rule once, as a reusable `Annotated` type in `models/base.py`,
  and choose between models with a `Literal` discriminator instead of `if` chains.
- Public modules, classes, and functions have Google-style docstrings; models list their
  fields under `Attributes:`.
- Mark overrides with `@override`. Raise subclasses of `curupira.errors.DispatchError`
  for expected failures.
- Never add a blanket `# noqa` or `# type: ignore`; scope a suppression to one rule and
  justify it in a comment, or fix the code.

```python
class CursorCliProfile(CliProfileBase):
    """Cursor options, including native agent/ask/plan mode selection.

    Attributes:
        provider: Discriminator identifying the Cursor CLI.
        agent: Optional Cursor execution mode.
    """

    provider: Literal["cursor"] = "cursor"
    agent: Literal["agent", "ask", "plan"] | None = None


CliProfile = Annotated[
    OpenCodeCliProfile | CodexCliProfile | ClaudeCodeCliProfile | CursorCliProfile,
    Field(discriminator="provider"),
]
```

## Testing

- Cover every behavior change with a test next to the matching module in `tests/`.
- Use the fakes in `tests/fakes.py`. Tests never hit the network, call real `gh`/`az`, or
  start authenticated coding agents.
- Warnings are errors (`filterwarnings = ["error"]`); fix their cause.
- Changes to `curupira.example.toml` must keep `test_example_configuration_is_valid` green.

## Security

- Start processes only through `AsyncProcessRunner` with a `CommandRequest`: an argument
  vector, never a shell string, with a timeout and bounded output.
- Pass every SQL value as a bound parameter; identifiers come from private constants.
- Validate user-supplied paths and repository names against traversal (`..`, absolute paths).
- Never commit, log, or echo secrets or tokens. Curupira does not manage authentication;
  provider CLIs own it.
- Investigate `uv run pip-audit` findings instead of ignoring them.

## Boundaries

- **Always:** run the commands above before finishing; update `CHANGELOG.md`
  (`Unreleased`), `docs/en/`, and this file in the same change when behavior, commands,
  or structure change.
- **Ask first:** adding or upgrading dependencies, editing `.github/workflows/`, and
  breaking changes to the TOML configuration schema or CLI.
- **Never:** hand-edit `uv.lock` (use `uv lock`), bump `src/curupira/_version.py` or push
  tags, weaken lint, type, or coverage gates, or edit `docs/pt/`/`docs/es/` without the
  English source.

## Git workflow

Branch from `main`, keep one logical change per commit, and open a pull request that
fills in [.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md).

## Definition of done

The pull request checklist is satisfied: tests cover the change, every command in
[Commands](#commands) passes, and user-facing documentation and the changelog are updated.
