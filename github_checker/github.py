"""Fetch repository state via the gh CLI."""

from typing import Any

from github_checker.models import Branch, PullRequest

DEPENDABOT_LOGIN = "dependabot[bot]"
COPILOT_LOGIN = "copilot-pull-request-reviewer[bot]"


def parse_pull(data: dict[str, Any]) -> PullRequest:
    """Map one item of GET repos/{r}/pulls to a model."""
    login = data["user"]["login"]
    return PullRequest(
        number=data["number"],
        title=data["title"],
        author=login,
        head_branch=data["head"]["ref"],
        is_dependabot=login == DEPENDABOT_LOGIN,
    )


def parse_branches(data: list[dict[str, Any]]) -> list[Branch]:
    """Map GET repos/{r}/branches to models."""
    return [Branch(name=item["name"]) for item in data]


def copilot_state(reviews: list[dict[str, Any]]) -> str | None:
    """Return the state of Copilot's latest review, or None."""
    states = [
        r["state"] for r in reviews if r.get("user", {}).get("login") == COPILOT_LOGIN
    ]
    return states[-1] if states else None


def count_copilot_comments(comments: list[dict[str, Any]]) -> int:
    """Count review comments authored by Copilot."""
    return sum(1 for c in comments if c.get("user", {}).get("login") == COPILOT_LOGIN)
