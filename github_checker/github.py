"""Fetch repository state via the gh CLI."""

import asyncio
import json
import re
import subprocess
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from github_checker.localgit import local_status
from github_checker.models import (
    Branch,
    CopilotReview,
    PullRequest,
    RepoRef,
    RepoState,
    RulesetDetails,
    RulesetInfo,
)

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


def parse_ruleset_info(data: dict[str, Any]) -> RulesetInfo:
    """Map one item of GET repos/{r}/rulesets to a model."""
    return RulesetInfo(
        id=data["id"],
        name=data["name"],
        enforcement=data["enforcement"],
        target=data.get("target", "branch"),
    )


def format_bypass_actor(actor: dict[str, Any]) -> str:
    """Human-readable bypass actor, e.g. 'admin (role), always'."""
    actor_type = actor.get("actor_type", "?")
    actor_id = actor.get("actor_id")
    mode = actor.get("bypass_mode", "always")
    if actor_type == "RepositoryRole":
        base = "admin (role)" if actor_id == 5 else f"role id={actor_id}"
    elif actor_type == "Integration":
        base = f"app id={actor_id}"
    elif actor_type == "Team":
        base = f"team id={actor_id}"
    elif actor_type == "OrganizationAdmin":
        base = "org admin"
    else:
        base = f"{actor_type} id={actor_id}"
    return f"{base}, {mode}"


def parse_ruleset_details(data: dict[str, Any]) -> RulesetDetails:
    """Map GET repos/{r}/rulesets/{id} to a model."""
    ref = (data.get("conditions") or {}).get("ref_name") or {}
    return RulesetDetails(
        id=data["id"],
        name=data["name"],
        enforcement=data["enforcement"],
        target=data.get("target", "branch"),
        include=ref.get("include", []),
        exclude=ref.get("exclude", []),
        rules=[rule["type"] for rule in data.get("rules", [])],
        bypass=[format_bypass_actor(a) for a in data.get("bypass_actors", [])],
    )


MAX_CONCURRENCY = 8
_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")


class GhError(Exception):
    """A failed gh CLI invocation."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


async def _gh_api(
    path: str, method: str = "GET", body: dict[str, Any] | None = None
) -> Any:
    """Run `gh api <path>` and return parsed JSON (None on empty output)."""
    args = ["api", path]
    if method != "GET":
        args += ["-X", method]
    stdin_data: bytes | None = None
    if body is not None:
        args += ["--input", "-"]
        stdin_data = json.dumps(body).encode()
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(stdin_data)
    if proc.returncode != 0:
        text = stderr.decode().strip()
        match = _HTTP_STATUS_RE.search(text)
        status = int(match.group(1)) if match else None
        raise GhError(status, text or "gh api failed")
    if not stdout.strip():
        return None
    return json.loads(stdout)


async def fetch_repo(ref: RepoRef, sem: asyncio.Semaphore) -> RepoState:
    """Fetch full state of one repository; errors go into RepoState.error."""
    name = ref.name
    local = (
        await asyncio.to_thread(local_status, ref.path)
        if ref.path is not None
        else None
    )

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
        reviewed = [
            (pull, state)
            for pull, reviews in zip(pulls, reviews_json)
            if (state := copilot_state(reviews)) is not None
        ]
        comments_json = await asyncio.gather(
            *(
                call(f"repos/{name}/pulls/{pull.number}/comments?per_page=100")
                for pull, _ in reviewed
            )
        )
        for (pull, state), comments in zip(reviewed, comments_json):
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
        try:
            rulesets_json = await call(f"repos/{name}/rulesets?per_page=100")
            rulesets: list[RulesetInfo] | None = [
                parse_ruleset_info(item) for item in rulesets_json
            ]
        except GhError:
            rulesets = None
        return RepoState(
            name=name,
            path=ref.path,
            local=local,
            pulls=pulls,
            branches=parse_branches(branches_json),
            alerts=alerts,
            rulesets=rulesets,
            updated_at=datetime.now(),
        )
    except GhError as err:
        return RepoState(name=name, path=ref.path, local=local, error=err.message)
    # Isolation: one repo must never kill the whole batch.
    except Exception as err:
        return RepoState(
            name=name,
            path=ref.path,
            local=local,
            error=f"{type(err).__name__}: {err}",
        )


async def fetch_all(repos: Sequence[RepoRef | str]) -> list[RepoState]:
    """Fetch all repositories concurrently (bounded by MAX_CONCURRENCY)."""
    refs = [r if isinstance(r, RepoRef) else RepoRef(name=r) for r in repos]
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    return list(await asyncio.gather(*(fetch_repo(r, sem) for r in refs)))


def gh_ready() -> str | None:
    """Return None if gh CLI is installed and authenticated, else a message."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=10
        )
    except FileNotFoundError:
        return "gh CLI не найден. Установите его: https://cli.github.com"
    except subprocess.TimeoutExpired:
        return "gh auth status не ответил за 10 секунд."
    if result.returncode != 0:
        return "gh не авторизован. Выполните `gh auth login`.\n" + result.stderr.strip()
    return None


_COPY_STRIP_FIELDS = frozenset(
    {
        "id",
        "source",
        "source_type",
        "created_at",
        "updated_at",
        "current_user_can_bypass",
        "node_id",
        "_links",
    }
)


async def list_rulesets(repo: str) -> list[RulesetInfo]:
    """List rulesets of a repository."""
    data = await _gh_api(f"repos/{repo}/rulesets?per_page=100")
    return [parse_ruleset_info(item) for item in data]


async def get_ruleset(repo: str, ruleset_id: int) -> RulesetDetails:
    """Fetch full details of one ruleset."""
    return parse_ruleset_details(await _gh_api(f"repos/{repo}/rulesets/{ruleset_id}"))


async def set_ruleset_enforcement(repo: str, ruleset_id: int, enforcement: str) -> None:
    """Set enforcement ('active' | 'disabled') of a ruleset."""
    await _gh_api(
        f"repos/{repo}/rulesets/{ruleset_id}",
        method="PUT",
        body={"enforcement": enforcement},
    )


async def delete_ruleset(repo: str, ruleset_id: int) -> None:
    """Delete a ruleset."""
    await _gh_api(f"repos/{repo}/rulesets/{ruleset_id}", method="DELETE")


def build_ruleset_copy(data: dict[str, Any]) -> dict[str, Any]:
    """Strip server-side fields from a ruleset body before POSTing a copy."""
    return {k: v for k, v in data.items() if k not in _COPY_STRIP_FIELDS}


async def copy_ruleset(src_repo: str, ruleset_id: int, dst_repo: str) -> None:
    """Copy a ruleset to another repository (retry with ' (copy)' on 422)."""
    data = await _gh_api(f"repos/{src_repo}/rulesets/{ruleset_id}")
    body = build_ruleset_copy(data)
    try:
        await _gh_api(f"repos/{dst_repo}/rulesets", method="POST", body=body)
    except GhError as err:
        if err.status != 422:
            raise
        retry_body = {**body, "name": f"{body['name']} (copy)"}
        await _gh_api(f"repos/{dst_repo}/rulesets", method="POST", body=retry_body)
