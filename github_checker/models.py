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
