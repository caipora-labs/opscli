"""Validated GitHub CLI boundary payloads."""

from pydantic import BaseModel, ConfigDict, Field

from curupira.models.base import NonEmptyString, ValidatedModel

DEFAULT_ISSUE_JSON_FIELDS = ("number", "title", "body", "url", "state", "labels")


class GhIssueSearchRequest(ValidatedModel):
    """A GitHub issue query."""

    repo: NonEmptyString
    query: NonEmptyString
    limit: int = Field(default=1, ge=1, le=1000)


class GhTaskViewRequest(ValidatedModel):
    """Address one GitHub issue or pull request for current-state validation."""

    repo: NonEmptyString
    number: int = Field(gt=0)


class GhPullRequestSearchRequest(GhIssueSearchRequest):
    """A GitHub pull request query."""

    jq: NonEmptyString | None = None


class GhLabel(BaseModel):
    """A GitHub label; tolerate additional upstream response fields."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    name: str


class GhRepositoryReference(BaseModel):
    """Repository metadata nested in a closing-issue reference."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)
    name_with_owner: str | None = Field(default=None, validation_alias="nameWithOwner")


class GhIssueReference(BaseModel):
    """An issue that a pull request will close when merged."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    number: int = Field(gt=0)
    repository: GhRepositoryReference | None = None


class GhStatusCheck(BaseModel):
    """One status-context or check-run result from a pull request."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    conclusion: str | None = None
    state: str | None = None


class GhIssue(BaseModel):
    """An issue received from GitHub."""

    model_config = ConfigDict(extra="ignore", frozen=True)
    number: int = Field(gt=0)
    title: str
    body: str | None = None
    url: str
    state: str | None = None
    labels: list[GhLabel] = Field(default_factory=list)


class GhPullRequest(GhIssue):
    """A pull request with branch metadata."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)
    is_draft: bool | None = Field(default=None, validation_alias="isDraft")
    head_ref_name: str | None = Field(default=None, validation_alias="headRefName")
    base_ref_name: str | None = Field(default=None, validation_alias="baseRefName")
    head_ref_oid: str | None = Field(default=None, validation_alias="headRefOid")
    mergeable: str | None = None
    merge_state_status: str | None = Field(default=None, validation_alias="mergeStateStatus")
    review_decision: str | None = Field(default=None, validation_alias="reviewDecision")
    status_check_rollup: list[GhStatusCheck] = Field(
        default_factory=list, validation_alias="statusCheckRollup"
    )
    closing_issues_references: list[GhIssueReference] = Field(
        default_factory=list, validation_alias="closingIssuesReferences"
    )
    merged_at: str | None = Field(default=None, validation_alias="mergedAt")
