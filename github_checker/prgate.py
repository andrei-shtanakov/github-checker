"""The merge gate: read a pull request, judge it, and merge it fail-closed.

`pr_detail` is a *view*; `merge_pr` is an independent enforcement point that
re-reads state and re-evaluates every predicate immediately before merging.
A caller must never be able to widen the gate by passing a stale payload.
"""

from typing import Any

from github_checker.models import (
    ChangedFile,
    CheckRun,
    PrDetail,
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
