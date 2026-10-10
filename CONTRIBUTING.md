# Contributing

**Curupira** (by Caipora Labs) is the product name. The installable package, primary console script, and Python import are `curupira`. The short CLI alias is `curu`.

## Environment setup

Requirements: Python 3.11+, [`uv`](https://docs.astral.sh/uv/), and `gh` for live
discovery (tests use fakes and need no authentication). The package is pure Python;
building a wheel needs no compiler toolchain.

```bash
uv sync --dev
uv run --no-sync prek install
```

`uv sync` installs the Python package and the `curupira` and `curu` console scripts.
[`prek`](https://prek.j178.dev/) replaces `pre-commit` for Git hooks: after `prek install`,
each commit runs the hooks in [`.pre-commit-config.yaml`](.pre-commit-config.yaml). Ruff and
Pyrefly hooks call `uv run --no-sync`, so they use the same locked versions and project
environment as the commands in [AGENTS.md](AGENTS.md). Run every hook on demand with
`uv run --no-sync prek run --all-files`.

[AGENTS.md](AGENTS.md) is the single source for the verification commands, repository
map, code style, testing rules, and security boundaries. It is written for coding agents
and humans alike; every command listed there must pass before opening a pull request, and
CI runs the same steps. Install the documentation tools with `uv sync --group docs`, then
preview the site with `uv run mkdocs serve`.

## Packaging

Hatchling is the PEP 517 backend. It reads the version from `src/curupira/_version.py`
and keeps the `curupira` and `curu` script entry points. `uv build` produces a
pure-Python `py3-none-any` wheel and an sdist. CLI wrappers (`gh`, coding-agent
CLIs, and Azure CLI) are invoked through `AsyncProcessRunner` using
`asyncio.create_subprocess_exec` with bounded capture, timeouts, and process-group
cleanup on POSIX.

## Extending Curupira

Curupira separates task discovery (`tasks/`), repository version control (`vcs/`), and
coding-agent CLI invocation (`agents/`) into layers whose contracts live in each
`base.py`; see the repository map in [AGENTS.md](AGENTS.md). Curupira does not manage
authentication: provider CLIs and the user's environment provide their own.

To add a task source, implement `TaskSource` and a `Trigger` that declares its
`trigger_type` and Pydantic `configuration_model`. Sources specific to one service or
company belong in a separate distribution that registers the trigger under the
`curupira.triggers` entry-point group and imports only `curupira.plugins`; see the
[plugin guide](docs/en/plugins.md). A built-in trigger lives in its own module under
`tasks/`, is imported by `tasks/__init__.py`, and deserves a dedicated issue/PR after the
task layer's `base.py` contract. A new version-control provider implements `VersionControl.clone`
and belongs in its own issue/PR after `vcs/base.py`. A new coding-agent adapter
implements `CodingAgentCliAdapter.build_arguments`, declares its `provider`,
`profile_model`, `display_name`, and `install_url`, and calls `register` from
`agents/registry.py` at the bottom of its module. When its CLI reports the session in a
shape other than a `sessionID`, `session_id`, or `thread_id` JSON field, the adapter
overrides `session_id_from_line`; when the CLI instead accepts a caller-chosen session
ID, it sets `assigns_session_id = True` and passes `request.new_session_id` to the CLI.
When the final answer is not a shape the shared `render_output` already understands, it
overrides `render_output`. Adapters never start processes or handle timeouts and output
limits themselves; `run_task` and `AsyncProcessRunner` own that. A built-in adapter lives
in its own module under `agents/` and is imported by `agents/__init__.py`, so `create_cli_adapter`
and profile validation find it through the registry; it belongs in its own issue/PR after
`agents/base.py`. Third-party adapters register under the `curupira.agents` entry-point
group instead. A trigger can
supply its own clone mechanism through `Trigger.create_version_control`. Azure DevOps
pull-request listing is supported via `azure-cli-pull-requests`, and monday.com board-item
discovery via `monday-cli-items` and the official `mcli` CLI; cloning still uses the GitHub
CLI version-control adapter unless `path` points at an existing checkout. Trello card
discovery is built in through Scale-Flow's `trello-cli`. Configuration accepts only the
trigger types and agent providers registered by built-ins and installed plugins.

When adding a provider, add its adapter in `src/curupira/agents/`, a page at
`docs/en/providers/<provider>.md`, and one line under "Providers and agents" in the
`mkdocs.yml` nav. Keep that nav line and the provider's entry in the README "Providers and
native options" list in alphabetical order by display name. The provider table on
`docs/en/providers.md` and the coding-agent CLIs in the installation requirements are
generated from the agent registry (`display_name`, `executable`, and `install_url`) during
the MkDocs build, so they update automatically. Other tools, such as forge CLIs, are listed
by hand in `docs/data/requirements.toml`.

## Documentation translations

Edit the canonical English pages in `docs/en/` first. When English documentation changes,
open a follow-up pull request to synchronize the corresponding Portuguese (`docs/pt/`) and
Spanish (`docs/es/`) pages. Generated Pydantic reference stays canonical in English; other
languages should link to it rather than manually translating generated fields.

## Documentation brand tokens

The docs theme uses the Caipora Labs palette. These hex values are the brand tokens.
Do not add other brand colors without a new decision. The Material overrides live in
`docs/stylesheets/extra.css`.

| Token | Hex | Role |
| --- | --- | --- |
| `primary` | `#F7931F` | orange brand |
| `primary-deep` | `#EA6114` | contrast / CTAs |
| `skin` | `#8E4F26` | Caipora brown |
| `accent` | `#39873B` | leaf / success |
| `neutral-0` | `#FEFDFC` | background |
| `neutral-900` | `#1A1A1A` | text |

On the light scheme, the header uses `primary` with `neutral-900` text, and primary
buttons use `primary-deep`. Body links use `skin`, which stays readable on `neutral-0`.
The leaf `accent` is the hover color. The dark scheme swaps the neutrals and uses
`primary` for links. The optional product accent (`#014FC9` / `#011E58`) is not
applied on the docs theme.

## Dependency audits

`pip-audit` runs in CI against the synced development environment:

```bash
uv run pip-audit
```

Investigate every finding: upgrade the affected constraint in `pyproject.toml`,
re-sync the lockfile, and re-run the full verification suite. If a finding is not
exploitable in this project (for example, a dev-only tool with no network path to
untrusted input), document the reason in the pull request instead of adding a
permanent ignore.

## Releases

Versioning is `MAJOR.MINOR.PATCH`. The single version source is
`src/curupira/_version.py`; the build backend reads it, and the CLI reports it.
Built wheels and sdists use that string as-is, so the Git tag and the file match
character for character after the tag's leading `v`.

### PyPI development rehearsal

Tag `vX.Y.Z.devN` publishes package `X.Y.Z.devN` (PEP 440) to PyPI. Bump the version
in `src/curupira/_version.py`, commit that change, and tag the same commit. Tag
`v0.1.0.dev0` already exists and must not be reused. The next rehearsal is
`0.1.0.dev1` with tag `v0.1.0.dev1`.

1. Set `__version__` to the next unused `X.Y.Z.devN` and commit.
2. Wait until CI is green on that commit. The tag workflow publishes only after the
   Ubuntu CI checks (lint, type check, Python test matrix, and distribution smoke
   tests) have succeeded for the tagged SHA.
3. Tag that commit and push the tag:

```bash
git tag v0.1.0.dev1
git push origin v0.1.0.dev1
```

`publish.yml` builds a pure-Python wheel and sdist once, then install-smoke-tests that
wheel on `ubuntu-latest` with `curupira --config curupira.example.toml validate`. The
publish job checks that the distribution version equals the tag without its leading
`v` and uploads with Trusted Publishing (`id-token: write`, environment `pypi`). Use
the canonical dotted form `vX.Y.Z.devN`. PyPI keeps an uploaded file, so each
rehearsal needs a new suffix.

Tags that contain `.dev` do not open a GitHub Release (`release.yml` still skips
them). `testpypi.yml` is unchanged: the same `v*.dev*` tags, and a manual
`workflow_dispatch` from `main`, still target TestPyPI. The rehearsal that gates the
first stable publish is the real PyPI upload from `publish.yml`.

Stable `vX.Y.Z` tags keep the production path below. Issue #103 publishes `0.1.0`
from a commit whose `__version__` is `0.1.0`.

To cut a release:

1. Move the `Unreleased` entries in `CHANGELOG.md` into a new version section.
2. Bump `__version__` in `src/curupira/_version.py` to the stable `X.Y.Z` version.
3. Run the full verification suite and confirm `uv build` plus
   `uv run --no-sync twine check dist/*` pass.
4. Rehearse with a `vX.Y.Z.devN` tag on PyPI (see above) before the first production
   publication.
5. Tag the validated commit as `vX.Y.Z` and push the tag. The `publish.yml` workflow
   publishes that version to PyPI; the `release.yml` workflow attaches the distributions
   to the matching GitHub release.

PyPI publishing uses Trusted Publishing (OIDC), so no API tokens are stored. Before
publishing, a PyPI maintainer registers this repository as a trusted publisher for the
`curupira` project with GitHub owner `caipora-labs`, repository `curupira`, workflow
filename `publish.yml`, and environment `pypi`. Development tags `vX.Y.Z.devN` use
that same publisher. `testpypi.yml` remains a separate workflow with environment
`testpypi`, workflow filename `testpypi.yml`, and audience `testpypi`. It uploads to
`https://test.pypi.org/legacy/` and smoke-tests that installation. The publish
workflow checks that the Ubuntu CI checks (lint, type check, Python test matrix,
and distribution smoke tests) succeeded for the commit
(`scripts/require_ci_checks.py`).
