"""Snapshot contract v2: the v1 planes + epics/v1 classification + attribution.

What v2 adds over v1 (`contracts/snapshot/v2/`, frozen like v1):

* every open Issue and PullRequest carries the normalized epics/v1
  classification object — never the raw body (volume, PII, prompt-injection
  surface stay out of the snapshot by design);
* a merged-PR attribution window: `commit → PR` restored from the GitHub API,
  so robin never has to guess from `#123` subject conventions.

Contract boundary, recorded in the v2 README and repeated here on purpose:
dispatcher consumes only the open issue/PR planes; the merged-PR window is
transport for robin, never state.

The v1 models in `snapshot.py`/`models.py` are frozen and untouched — v2
ships alongside, and the CLI emits v1 by default until consumers migrate.
"""

import asyncio
import socket
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from github_checker import github
from github_checker.epics import (
    Carrier,
    EpicClassification,
    artifact_uri,
    classify_body,
)
from github_checker.github import (
    DEPENDABOT_LOGIN,
    GhError,
    copilot_reviews,
    gh_ready,
    parse_branches,
    parse_ruleset_info,
)
from github_checker.localgit import local_status, remote_url
from github_checker.models import (
    Branch,
    CopilotReview,
    LocalStatus,
    RepoRef,
    RulesetInfo,
)
from github_checker.snapshot import discover, parse_github_remote

WINDOW_DAYS_DEFAULT = 30
# Every listing call caps at one page; hitting the cap is reported as
# truncation, never silently read as completeness (the issue-lookup idiom).
PAGE_CAP = 100


class PullRequestV2(BaseModel):
    """An open pull request with its epics/v1 classification."""

    number: int
    title: str
    author: str
    head_branch: str
    is_dependabot: bool
    copilot_review: CopilotReview | None = None
    epic: EpicClassification


class IssueV2(BaseModel):
    """An open issue (pull requests excluded) with its classification."""

    number: int
    title: str
    author: str
    labels: list[str] = []
    epic: EpicClassification


class MergedPullRequest(BaseModel):
    """One merged PR of the attribution window: `commit → PR` without
    heuristics — the SHAs are the projection, never the `#123` convention."""

    number: int
    merge_commit_sha: str | None
    commit_shas: list[str]
    commit_shas_truncated: bool
    merged_at: datetime
    epic: EpicClassification


class MergedPrWindow(BaseModel):
    """Attribution transport for robin; dispatcher must not read it as state.

    `truncated` is explicit so a cut-off window is never mistaken for an
    empty one: True means merged PRs inside the window may exist unseen.
    """

    window_days: int
    truncated: bool
    prs: list[MergedPullRequest] = []


class RepoStateV2(BaseModel):
    """Everything the snapshot publishes about one repository (v2)."""

    name: str
    pulls: list[PullRequestV2] = []
    issues: list[IssueV2] | None = None
    branches: list[Branch] = []
    alerts: int | None = None
    rulesets: list[RulesetInfo] | None = None
    merged: MergedPrWindow | None = None
    error: str | None = None
    updated_at: datetime | None = None
    path: Path | None = None
    local: LocalStatus | None = None


class RepoSnapshotV2(BaseModel):
    """One workspace repository: local git state plus optional GitHub state."""

    dir: str
    remote: str | None
    local: LocalStatus
    github: RepoStateV2 | None = None


class WorkspaceSnapshotV2(BaseModel):
    """Full fleet state, contract v2.

    The JSON shape is a frozen contract (`contracts/snapshot/v2/`): consumers
    key off `schema_version`, and any breaking change must ship as v3
    alongside v2 — never as an edit to v2.
    """

    schema_version: Literal[2] = 2
    workspace: Path
    host: str
    generated_at: datetime
    gh_error: str | None
    repos: list[RepoSnapshotV2]


def _classify_item(
    item: dict[str, Any],
    name: str,
    kind: Literal["pull", "issues"],
    carrier: Carrier,
    observed_at: str,
) -> EpicClassification:
    """Normalize one listing payload; a payload without a `body` key is
    `unavailable` (not read), a null body is an empty body (read)."""
    return classify_body(
        item.get("body"),
        retrieved="body" in item,
        subject_uri=artifact_uri(name, kind, item["number"]),
        carrier=carrier,
        observed_at=observed_at,
    )


def parse_pull_v2(item: dict[str, Any], name: str, observed_at: str) -> PullRequestV2:
    """Map one item of GET repos/{r}/pulls?state=open to a v2 model."""
    login = item["user"]["login"]
    return PullRequestV2(
        number=item["number"],
        title=item["title"],
        author=login,
        head_branch=item["head"]["ref"],
        is_dependabot=login == DEPENDABOT_LOGIN,
        epic=_classify_item(item, name, "pull", "pull_request", observed_at),
    )


def parse_issues_v2(
    data: list[dict[str, Any]], name: str, observed_at: str
) -> list[IssueV2]:
    """Map GET repos/{r}/issues to v2 models, skipping pull requests."""
    return [
        IssueV2(
            number=item["number"],
            title=item["title"],
            author=item["user"]["login"],
            labels=[label["name"] for label in item.get("labels", [])],
            epic=_classify_item(item, name, "issues", "issue", observed_at),
        )
        for item in data
        if "pull_request" not in item
    ]


def _updated_at_inside(item: dict[str, Any], cutoff: datetime) -> bool:
    """Whether an item's `updated_at` is at/after *cutoff*, failing closed.

    Used only for the truncation verdict: a missing or unparseable timestamp
    is *unknown*, and unknown must not read as «the window was covered», so
    both answer True. `merged_at` is deliberately NOT treated this way — it
    is published data, and a malformed value fails the repo loudly (the
    per-repo error isolation) rather than being guessed at.
    """
    raw = item.get("updated_at")
    if raw is None:
        return True
    try:
        return datetime.fromisoformat(raw) >= cutoff
    except ValueError:
        return True


def merged_in_window(
    page: list[dict[str, Any]], cutoff: datetime
) -> tuple[list[dict[str, Any]], bool]:
    """Merged PRs of one closed-PR page inside the window, plus honesty flag.

    The page is sorted by `updated` descending and `updated_at >= merged_at`
    always holds, so: if the page is full AND its oldest-updated item is still
    inside the window, older merged-in-window PRs may exist beyond the page —
    `truncated=True`. An unparseable/missing `updated_at` fails closed the
    same way: unknown must not read as covered.
    """
    merged = [
        item
        for item in page
        if item.get("merged_at") and datetime.fromisoformat(item["merged_at"]) >= cutoff
    ]
    truncated = len(page) >= PAGE_CAP and _updated_at_inside(page[-1], cutoff)
    return merged, truncated


async def fetch_repo_v2(
    ref: RepoRef,
    sem: asyncio.Semaphore,
    *,
    window_days: int = WINDOW_DAYS_DEFAULT,
    now: datetime | None = None,
) -> RepoStateV2:
    """Fetch v2 state of one repository; errors go into RepoStateV2.error."""
    name = ref.name
    now = now if now is not None else datetime.now().astimezone()
    observed_at = now.isoformat()
    cutoff = now - timedelta(days=window_days)
    local = (
        await asyncio.to_thread(local_status, ref.path)
        if ref.path is not None
        else None
    )

    async def call(path: str) -> Any:
        async with sem:
            return await github._gh_api(path)

    try:
        pulls_json, branches_json, closed_json = await asyncio.gather(
            call(f"repos/{name}/pulls?state=open&per_page=100"),
            call(f"repos/{name}/branches?per_page=100"),
            call(
                f"repos/{name}/pulls"
                "?state=closed&sort=updated&direction=desc&per_page=100"
            ),
        )
        pulls = [parse_pull_v2(item, name, observed_at) for item in pulls_json]
        by_number = await copilot_reviews(name, [p.number for p in pulls], call)
        for pull in pulls:
            pull.copilot_review = by_number.get(pull.number)

        merged_items, truncated = merged_in_window(closed_json, cutoff)
        merged_items = sorted(merged_items, key=lambda item: item["number"])
        commits_json = await asyncio.gather(
            *(
                call(f"repos/{name}/pulls/{item['number']}/commits?per_page=100")
                for item in merged_items
            )
        )
        merged = MergedPrWindow(
            window_days=window_days,
            truncated=truncated,
            prs=[
                MergedPullRequest(
                    number=item["number"],
                    merge_commit_sha=item.get("merge_commit_sha"),
                    commit_shas=[c["sha"] for c in commits],
                    commit_shas_truncated=len(commits) >= PAGE_CAP,
                    merged_at=item["merged_at"],
                    epic=_classify_item(
                        item, name, "pull", "pull_request", observed_at
                    ),
                )
                for item, commits in zip(merged_items, commits_json)
            ],
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
        try:
            issues_json = await call(f"repos/{name}/issues?state=open&per_page=100")
            issues: list[IssueV2] | None = parse_issues_v2(
                issues_json, name, observed_at
            )
        except GhError:
            issues = None
        return RepoStateV2(
            name=name,
            path=ref.path,
            local=local,
            pulls=pulls,
            issues=issues,
            branches=parse_branches(branches_json),
            alerts=alerts,
            rulesets=rulesets,
            merged=merged,
            updated_at=now,
        )
    except GhError as err:
        return RepoStateV2(name=name, path=ref.path, local=local, error=err.message)
    # Isolation: one repo must never kill the whole batch.
    except Exception as err:
        return RepoStateV2(
            name=name,
            path=ref.path,
            local=local,
            error=f"{type(err).__name__}: {err}",
        )


async def fetch_all_v2(
    repos: Sequence[RepoRef | str],
    *,
    window_days: int = WINDOW_DAYS_DEFAULT,
    now: datetime | None = None,
) -> list[RepoStateV2]:
    """Fetch all repositories concurrently (bounded like the v1 path)."""
    refs = [r if isinstance(r, RepoRef) else RepoRef(name=r) for r in repos]
    sem = asyncio.Semaphore(github.MAX_CONCURRENCY)
    return list(
        await asyncio.gather(
            *(fetch_repo_v2(r, sem, window_days=window_days, now=now) for r in refs)
        )
    )


async def build_snapshot_v2(
    root: Path,
    include_github: bool = True,
    window_days: int = WINDOW_DAYS_DEFAULT,
) -> WorkspaceSnapshotV2:
    """Collect local state for every repo, plus GitHub v2 state when gh is
    ready — the v1 pipeline with the v2 fetch path swapped in."""
    root = root.resolve()
    dirs = discover(root)
    locals_ = await asyncio.gather(*(asyncio.to_thread(local_status, d) for d in dirs))
    remotes = await asyncio.gather(*(asyncio.to_thread(remote_url, d) for d in dirs))
    slugs = [parse_github_remote(url) if url else None for url in remotes]

    gh_error = gh_ready() if include_github else "skipped (--local-only)"
    states: dict[str, RepoStateV2] = {}
    if gh_error is None:
        refs = [RepoRef(name=slug) for slug in slugs if slug]
        states = {
            state.name: state
            for state in await fetch_all_v2(refs, window_days=window_days)
        }

    repos = [
        RepoSnapshotV2(
            dir=d.name,
            remote=slug,
            local=local,
            github=states.get(slug) if slug else None,
        )
        for d, slug, local in zip(dirs, slugs, locals_)
    ]
    return WorkspaceSnapshotV2(
        workspace=root,
        host=socket.gethostname(),
        # tz-aware: contract schema declares format: date-time (strict RFC3339)
        generated_at=datetime.now().astimezone(),
        gh_error=gh_error,
        repos=repos,
    )
