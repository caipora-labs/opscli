# Curupira

Curupira runs automations on your machine. It takes a GitHub issue or pull request, a monday.com board item, or a local cron occurrence, and hands it to a coding-agent CLI you already have.

Each automation in the settings TOML watches one source and carries its own prompt. All automations share one discovery, scheduling, and execution pipeline: `run` drains currently available tasks, while `run --watch` polls every automation continuously.

## Get started

1. [Install Curupira and its requirements](installation.md).
2. [Configure automations](configuration.md) for issues, pull requests, monday.com items, or cron.
3. [Validate and run](operations.md) your configuration.

See [providers and agents](providers.md) for provider-native options and session resumption.
