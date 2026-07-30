"""The merge gate: read a pull request, judge it, and merge it fail-closed.

`pr_detail` is a *view*; `merge_pr` is an independent enforcement point that
re-reads state and re-evaluates every predicate immediately before merging.
A caller must never be able to widen the gate by passing a stale payload.
"""

import json
from pathlib import Path
from typing import Any

from github_checker.ghcli import run_gh
from github_checker.models import (
    ChangedFile,
    CheckRun,
    PrDetail,
    ReviewThread,
)

PR_VIEW_FIELDS = (
    "number,title,url,state,isDraft,mergeable,mergeStateStatus,"
    "headRefName,headRefOid,baseRefName,reviewDecision,statusCheckRollup,"
    "files,changedFiles"
)

_SUCCESSFUL_CHECKS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})


def _check_state(item: dict[str, Any]) -> str:
    """Flatten a check run or a legacy status context to one state word."""
    if item.get("__typename") == "StatusContext" or "context" in item:
        return str(item.get("state") or "PENDING")
    if item.get("status") != "COMPLETED":
        return "PENDING"
    return str(item.get("conclusion") or "PENDING")


def _check_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("context") or "?")


def parse_pr_view(data: dict[str, Any], *, file_limit: int) -> PrDetail:
    """Map `gh pr view --json PR_VIEW_FIELDS` output onto a PrDetail."""
    rollup = data.get("statusCheckRollup") or []
    raw_files = data.get("files") or []
    files = [
        ChangedFile(
            path=item["path"],
            additions=item.get("additions", 0),
            deletions=item.get("deletions", 0),
        )
        for item in raw_files[:file_limit]
    ]
    total = data.get("changedFiles") or len(raw_files)
    return PrDetail(
        number=data["number"],
        title=data.get("title", ""),
        url=data.get("url", ""),
        state=data.get("state", "UNKNOWN"),
        is_draft=bool(data.get("isDraft")),
        mergeable=data.get("mergeable") or "UNKNOWN",
        merge_state_status=data.get("mergeStateStatus"),
        head_branch=data.get("headRefName", ""),
        head_sha=data.get("headRefOid", ""),
        base_branch=data.get("baseRefName", ""),
        review_decision=data.get("reviewDecision") or None,
        checks=[
            CheckRun(name=_check_name(item), state=_check_state(item))
            for item in rollup
        ],
        files=files,
        files_total=total,
        files_truncated=len(raw_files) > file_limit or total > len(files),
    )


class GateUnavailable(Exception):
    """PR state could not be established; the gate must stay closed."""


THREAD_PAGE_SIZE = 100
MAX_THREAD_PAGES = 5
_EXCERPT_CHARS = 280

_THREADS_QUERY = (
    """
query($owner:String!,$name:String!,$number:Int!,$cursor:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      reviewThreads(first:%d, after:$cursor){
        pageInfo{ hasNextPage endCursor }
        nodes{
          id isResolved isOutdated path
          comments(first:1){ nodes{ author{ login } body } }
        }
      }
    }
  }
}
"""
    % THREAD_PAGE_SIZE
)


def _review_threads_connection(page: dict[str, Any]) -> dict[str, Any]:
    """The `reviewThreads` connection, or raise if it isn't positively there.

    A GraphQL error, or a break anywhere in data -> repository ->
    pullRequest -> reviewThreads, must not be read as "zero threads" — that
    is the empty-list-opens-the-gate inversion this verb exists to prevent.
    An actually-present connection (even with `nodes: []`) is a legitimate
    empty result and is returned as-is.
    """
    if "errors" in page:
        raise GateUnavailable(
            f"review-threads response carried errors: {page['errors']!r}"
        )
    data = page.get("data")
    if not isinstance(data, dict):
        raise GateUnavailable("review-threads response missing 'data'")
    repository = data.get("repository")
    if not isinstance(repository, dict):
        raise GateUnavailable("review-threads response missing 'repository'")
    pull_request = repository.get("pullRequest")
    if not isinstance(pull_request, dict):
        raise GateUnavailable("review-threads response missing 'pullRequest'")
    connection = pull_request.get("reviewThreads")
    if not isinstance(connection, dict):
        raise GateUnavailable("review-threads response missing 'reviewThreads'")
    return connection


def parse_review_threads(page: dict[str, Any]) -> tuple[list[ReviewThread], str | None]:
    """Map one GraphQL page to threads plus the next cursor (None if last)."""
    connection = _review_threads_connection(page)
    threads: list[ReviewThread] = []
    for node in connection.get("nodes") or []:
        comments = (node.get("comments") or {}).get("nodes") or []
        first = comments[0] if comments else {}
        body = first.get("body")
        threads.append(
            ReviewThread(
                id=node["id"],
                is_resolved=bool(node.get("isResolved")),
                is_outdated=bool(node.get("isOutdated")),
                path=node.get("path"),
                author=(first.get("author") or {}).get("login"),
                excerpt=body[:_EXCERPT_CHARS] if body else None,
            )
        )
    info = connection.get("pageInfo") or {}
    cursor = info.get("endCursor") if info.get("hasNextPage") else None
    return threads, cursor


def fetch_review_threads(
    path: Path, owner: str, name: str, number: int, *, binary: str = "gh"
) -> tuple[list[ReviewThread], bool]:
    """All review threads of a PR; the flag means the page cap cut the list."""
    threads: list[ReviewThread] = []
    cursor: str | None = None
    for _ in range(MAX_THREAD_PAGES):
        args = [
            "api",
            "graphql",
            "-f",
            f"query={_THREADS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={number}",
        ]
        if cursor is not None:
            args += ["-F", f"cursor={cursor}"]
        proc = run_gh(path, *args, binary=binary)
        if proc.returncode != 0:
            # неизвестное состояние тредов = закрытые ворота, а не пустой список
            raise GateUnavailable(proc.stderr.strip() or "gh api graphql failed")
        try:
            page = json.loads(proc.stdout)
        except json.JSONDecodeError as err:
            raise GateUnavailable("unexpected non-JSON from gh api graphql") from err
        batch, cursor = parse_review_threads(page)
        threads.extend(batch)
        if cursor is None:
            return threads, False
    return threads, True
