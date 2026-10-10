"""Task identities, resolved execution snapshots, and persistence contracts."""

import hashlib
import json
from collections.abc import Mapping
from enum import IntEnum
from pathlib import Path

from pydantic import AwareDatetime, Field, model_validator

from curupira.models.base import Identifier, NonEmptyString, ValidatedModel
from curupira.models.configuration import AutomationConfiguration
from curupira.models.profiles import CliProfile


class WorkflowStage(IntEnum):
    """Scheduler ordering from closest to completion to least advanced work."""

    READY_PULL_REQUEST = 0
    CONFLICTING_PULL_REQUEST = 1
    ACTIVE_PULL_REQUEST = 2
    DRAFT_PULL_REQUEST = 3
    ISSUE = 4
    OTHER = 5


class TaskIdentity(ValidatedModel):
    """One automation's work on a source item or scheduled occurrence."""

    automation_id: Identifier
    repo: NonEmptyString
    task_type: NonEmptyString
    id: NonEmptyString

    @property
    def key(self) -> str:
        """Return a collision-free canonical key shared by all consumers."""
        return json.dumps(
            [self.automation_id, self.repo, self.task_type, self.id], separators=(",", ":")
        )


class ResolvedAutomation(ValidatedModel):
    """A validated automation with effective profile, workspace, and timezone."""

    automation_id: Identifier
    configuration: AutomationConfiguration
    profile: CliProfile
    workspace_path: Path
    timezone: NonEmptyString | None = None


class Task(ValidatedModel):
    """Resolved task passed unchanged from discovery to execution and persistence.

    Attributes:
        identity: Canonical identity of the source item or occurrence.
        automation: Resolved automation snapshot that discovered the task.
        title: Human-readable task title.
        body: Optional task description.
        url: Link to the source item.
        is_draft: Whether a pull request is a draft, when applicable.
        head_ref_name: Pull-request source branch, when applicable.
        base_ref_name: Pull-request target branch, when applicable.
        head_sha: Current pull-request head commit, when available.
        mergeable: Current GitHub mergeability result, when available.
        merge_state_status: Current GitHub merge state, when available.
        check_conclusions: Remote check conclusions for the pull-request head.
        linked_issue_numbers: Issue numbers referenced by the pull request's closing metadata.
        linked_issue_states: Current states of those linked issues.
        scheduled_for: Cron occurrence, only for cron tasks.
        attributes: Source-specific string values that triggers expose to prompts.
    """

    identity: TaskIdentity
    automation: ResolvedAutomation
    title: str
    body: str | None = None
    url: str
    is_draft: bool | None = None
    head_ref_name: str | None = None
    base_ref_name: str | None = None
    head_sha: str | None = None
    mergeable: str | None = None
    merge_state_status: str | None = None
    check_conclusions: tuple[str, ...] = ()
    linked_issue_numbers: tuple[str, ...] = ()
    linked_issue_states: tuple[str, ...] = ()
    scheduled_for: AwareDatetime | None = None
    attributes: Mapping[str, str] = Field(default_factory=dict)

    @property
    def workflow_stage(self) -> WorkflowStage:
        """Return the current deterministic dispatch stage for this task."""
        if self.identity.task_type == "github-cli-pull-requests":
            if self.is_draft:
                return WorkflowStage.DRAFT_PULL_REQUEST
            if self.merge_state_status == "DIRTY" or self.mergeable == "CONFLICTING":
                return WorkflowStage.CONFLICTING_PULL_REQUEST
            checks_pass = bool(self.check_conclusions) and all(
                conclusion == "SUCCESS" for conclusion in self.check_conclusions
            )
            if self.mergeable == "MERGEABLE" and self.merge_state_status == "CLEAN" and checks_pass:
                return WorkflowStage.READY_PULL_REQUEST
            return WorkflowStage.ACTIVE_PULL_REQUEST
        if self.identity.task_type == "issue":
            return WorkflowStage.ISSUE
        return WorkflowStage.OTHER

    @property
    def logical_key(self) -> str:
        """Return an automation-independent key for a source item."""
        return json.dumps(
            [self.identity.repo, self.identity.task_type, self.identity.id], separators=(",", ":")
        )

    @property
    def dispatch_key(self) -> str:
        """Return the deduplication key, including PR head revisions."""
        if self.identity.task_type == "github-cli-pull-requests":
            return json.dumps(
                [
                    self.logical_key,
                    self.head_sha,
                    int(self.workflow_stage),
                    self.state_fingerprint,
                ],
                separators=(",", ":"),
            )
        return self.identity.key

    @property
    def state_key(self) -> str:
        """Return the durable completion key, shared across automations for one PR."""
        if self.identity.task_type == "github-cli-pull-requests":
            return self.logical_key
        return self.identity.key

    @property
    def state_fingerprint(self) -> str:
        """Return a stable fingerprint of source state relevant to repeating work."""
        state = json.dumps(
            {
                "title": self.title,
                "body": self.body,
                "url": self.url,
                "head_sha": self.head_sha,
                "is_draft": self.is_draft,
                "mergeable": self.mergeable,
                "merge_state_status": self.merge_state_status,
                "check_conclusions": self.check_conclusions,
                "linked_issue_numbers": self.linked_issue_numbers,
                "linked_issue_states": self.linked_issue_states,
                "workflow_stage": int(self.workflow_stage),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(state.encode()).hexdigest()

    @model_validator(mode="after")
    def validate_source(self) -> "Task":
        """Keep task identity consistent with its resolved automation and trigger."""
        config = self.automation.configuration
        if (
            self.identity.automation_id != self.automation.automation_id
            or self.identity.repo != config.repo
            or self.identity.task_type != config.trigger_type
        ):
            raise ValueError("task identity must match its resolved automation")
        from curupira.tasks.registry import get

        get(config.trigger_type).validate_task(self)
        return self


class RunningCodingSession(ValidatedModel):
    """The original task snapshot and native session required for resumption."""

    task: Task
    session_id: NonEmptyString
    message: NonEmptyString


class CompletedTaskState(ValidatedModel):
    """A completed source snapshot and the next external action, if any."""

    fingerprint: NonEmptyString
    next_action: NonEmptyString


class CronRunState(ValidatedModel):
    """A cron automation's creation time and claimed occurrence watermark."""

    automation_id: Identifier
    created_at: AwareDatetime
    last_execution_at: AwareDatetime | None = None
    last_scheduled_for: AwareDatetime | None = None
    pending_scheduled_for: AwareDatetime | None = None
