"""Fetch repository state via the gh CLI."""

import asyncio
import json
import re
import subprocess
from datetime import datetime
from typing import Any

from github_checker.models import Branch, CopilotReview, PullRequest, RepoState

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


MAX_CONCURRENCY = 8
_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")


class GhError(Exception):
    """A failed gh CLI invocation."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


async def _gh_api(path: str) -> Any:
    """Run `gh api <path>` and return parsed JSON."""
    proc = await asyncio.create_subprocess_exec(
        "gh",
        "api",
        path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        text = stderr.decode().strip()
        match = _HTTP_STATUS_RE.search(text)
        status = int(match.group(1)) if match else None
        raise GhError(status, text or "gh api failed")
    return json.loads(stdout)


async def fetch_repo(name: str, sem: asyncio.Semaphore) -> RepoState:
    """Fetch full state of one repository; errors go into RepoState.error."""

    async def call(path: str) -> Any:
        async with sem:
            return await _gh_api(path)

    try:
        pulls_json, branches_json = await asyncio.gather(
            call(f"repos/{name}/pulls?state=open&per_page=100"),
            call(f"repos/{name}/branches?per_page=100"),
        )
        pulls = [parse_pull(item) for item in pulls_json]
        reviews_json = await asyncio.gather(
            *(
                call(f"repos/{name}/pulls/{p.number}/reviews?per_page=100")
                for p in pulls
            )
        )
        for pull, reviews in zip(pulls, reviews_json):
            state = copilot_state(reviews)
            if state is None:
                continue
            comments = await call(
                f"repos/{name}/pulls/{pull.number}/comments?per_page=100"
            )
            pull.copilot_review = CopilotReview(
                state=state,
                comment_count=count_copilot_comments(comments),
            )
        try:
            alerts_json = await call(
                f"repos/{name}/dependabot/alerts?state=open&per_page=100"
            )
            alerts: int | None = len(alerts_json)
        except GhError as err:
            if err.status not in (403, 404):
                raise
            alerts = None
        return RepoState(
            name=name,
            pulls=pulls,
            branches=parse_branches(branches_json),
            alerts=alerts,
            updated_at=datetime.now(),
        )
    except GhError as err:
        return RepoState(name=name, error=err.message)
    # Isolation: one repo must never kill the whole batch.
    except Exception as err:
        return RepoState(name=name, error=f"{type(err).__name__}: {err}")


async def fetch_all(repos: list[str]) -> list[RepoState]:
    """Fetch all repositories concurrently (bounded by MAX_CONCURRENCY)."""
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    return list(await asyncio.gather(*(fetch_repo(r, sem) for r in repos)))


def gh_ready() -> str | None:
    """Return None if gh CLI is installed and authenticated, else a message."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True
        )
    except FileNotFoundError:
        return "gh CLI не найден. Установите его: https://cli.github.com"
    if result.returncode != 0:
        return "gh не авторизован. Выполните `gh auth login`.\n" + result.stderr.strip()
    return None
