"""Task sources and trigger implementations."""

# Import concrete triggers so registry lookups work regardless of which application
# entry point is used first.
from curupira.tasks import (  # noqa: F401
    azure_pull_requests,
    cron,
    github_issues,
    github_pull_requests,
    monday_items,
    trello_cards,
)
