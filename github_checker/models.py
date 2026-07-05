"""Pydantic models for config and repository state."""

import re
from datetime import datetime

from pydantic import BaseModel, field_validator

REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


class Config(BaseModel):
    """Application configuration stored in repos.toml."""

    repos: list[str] = []
    refresh_seconds: int = 120

    @field_validator("repos")
    @classmethod
    def _validate_repos(cls, value: list[str]) -> list[str]:
        for repo in value:
            if not REPO_RE.match(repo):
                raise ValueError(f"invalid repo (expected owner/repo): {repo!r}")
        return value


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


class RepoState(BaseModel):
    """Everything the TUI shows about one repository."""

    name: str
    pulls: list[PullRequest] = []
    branches: list[Branch] = []
    alerts: int | None = None
    rulesets: list[RulesetInfo] | None = None
    error: str | None = None
    updated_at: datetime | None = None
