"""Pydantic models for config and repository state."""

import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, field_validator

REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


class RepoRef(BaseModel):
    """A tracked repository: owner/repo plus an optional local clone path."""

    name: str
    path: Path | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not REPO_RE.match(value):
            raise ValueError(f"invalid repo (expected owner/repo): {value!r}")
        return value


class Config(BaseModel):
    """Application configuration stored in repos.toml."""

    repos: list[RepoRef] = []
    refresh_seconds: int = 120

    @field_validator("repos", mode="before")
    @classmethod
    def _coerce_repos(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [{"name": item} if isinstance(item, str) else item for item in value]


class Branch(BaseModel):
    """A git branch."""

    name: str


class CopilotReview(BaseModel):
    """Summary of GitHub Copilot's review on a pull request."""

    state: str
    comment_count: int


class PullRequest(BaseModel):
    """An open pull request."""

    number: int
    title: str
    author: str
    head_branch: str
    is_dependabot: bool
    copilot_review: CopilotReview | None = None


class Issue(BaseModel):
    """An open issue (pull requests excluded)."""

    number: int
    title: str
    author: str
    labels: list[str] = []


class IssueRef(BaseModel):
    """One inbox issue, with every field the authoring screen renders.

    Deliberately not an extension of `Issue`: that model belongs to the
    snapshot contract, which dispatcher vendors as a pinned copy.
    """

    number: int
    title: str
    state: str
    url: str
    author: str
    labels: list[str] = []


class RulesetInfo(BaseModel):
    """Item of GET repos/{r}/rulesets."""

    id: int
    name: str
    enforcement: str
    target: str


class RulesetDetails(BaseModel):
    """GET repos/{r}/rulesets/{id} — fields the protection screen needs."""

    id: int
    name: str
    enforcement: str
    target: str
    include: list[str]
    exclude: list[str]
    rules: list[str]
    bypass: list[str]


class LocalStatus(BaseModel):
    """State of a local clone relative to its upstream."""

    branch: str | None
    ahead: int | None
    behind: int | None
    dirty: bool
    error: str | None = None


class ChangedFile(BaseModel):
    """One file touched by a pull request."""

    path: str
    additions: int = 0
    deletions: int = 0


class CheckRun(BaseModel):
    """A normalised CI signal: check run and legacy status look the same here."""

    name: str
    state: str  # SUCCESS | FAILURE | PENDING | SKIPPED | ...


class ReviewThread(BaseModel):
    """A review conversation and whether it is still open."""

    id: str
    is_resolved: bool
    is_outdated: bool = False
    path: str | None = None
    author: str | None = None
    excerpt: str | None = None


class PrDetail(BaseModel):
    """Everything the merge gate reads about one pull request."""

    number: int
    title: str
    url: str
    state: str
    is_draft: bool
    mergeable: str
    merge_state_status: str | None = None
    head_branch: str
    head_sha: str
    base_branch: str
    review_decision: str | None = None
    checks: list[CheckRun] = []
    checks_truncated: bool = False
    files: list[ChangedFile] = []
    files_total: int = 0
    files_truncated: bool = False
    review_threads: list[ReviewThread] = []
    threads_truncated: bool = False
    diff: str | None = None
    diff_truncated: bool = False
    allows_squash: bool | None = None


class GateResult(BaseModel):
    """Which merge predicates failed; empty `failed` means the gate is open."""

    passed: bool
    failed: list[str] = []


class RepoState(BaseModel):
    """Everything the TUI shows about one repository."""

    name: str
    pulls: list[PullRequest] = []
    issues: list[Issue] | None = None
    branches: list[Branch] = []
    alerts: int | None = None
    rulesets: list[RulesetInfo] | None = None
    error: str | None = None
    updated_at: datetime | None = None
    path: Path | None = None
    local: LocalStatus | None = None
