"""Headless whitelist actions: CLI twins of the TUI keys `s`/`S` plus PR creation.

Consumers are programs (dispatcher's action endpoints), so every action prints
one JSON `ActionResult`. The whitelist is deliberately tiny and safe:
`pull` is fast-forward-only by construction; `open-pr` never pushes — it only
creates (or reports) a pull request for an already-pushed branch.
"""

import json
from pathlib import Path

from pydantic import BaseModel

from github_checker.ghcli import run_gh
from github_checker.localgit import (
    LocalGitError,
    default_branch,
    delete_branch,
    fetch,
    has_upstream,
    head_rev,
    is_detached,
    is_git_repo,
    local_status,
    merged_local_branches,
    pull_ff_only,
    set_head_auto,
    switch_branch,
    worktree_holding,
)
from github_checker.models import LocalStatus, PrDetail


class ActionResult(BaseModel):
    """Outcome of one headless action; the CLI prints this as JSON."""

    action: str
    dir: str
    ok: bool
    error: str | None = None
    detail: str | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    local: LocalStatus | None = None
    branch: str | None = None
    base_branch: str | None = None
    commit_sha: str | None = None
    changed_paths: list[str] | None = None
    merged: bool | None = None
    local_sync: str | None = None  # ok | failed | not_attempted | not_applicable
    gate_failed: list[str] | None = None
    pr_detail: PrDetail | None = None


def pull(path: Path) -> ActionResult:
    """`git pull --ff-only` in *path*; refuses non-repos, reports final state."""
    if not is_git_repo(path):
        return ActionResult(
            action="pull", dir=str(path), ok=False, error="not a git repository"
        )
    before = head_rev(path)
    try:
        pull_ff_only(path)
    except LocalGitError as err:
        return ActionResult(
            action="pull",
            dir=str(path),
            ok=False,
            error=str(err),
            local=local_status(path),
        )
    detail = "already up to date" if head_rev(path) == before else "fast-forwarded"
    return ActionResult(
        action="pull",
        dir=str(path),
        ok=True,
        detail=detail,
        local=local_status(path),
    )


def open_pr(path: Path) -> ActionResult:
    """Create a PR for the current branch via `gh pr create --fill`.

    Idempotent: if the branch already has an open PR, report it instead of
    failing. Never pushes — an unpushed branch is an error, not a side effect.
    """
    if not is_git_repo(path):
        return ActionResult(
            action="open-pr", dir=str(path), ok=False, error="not a git repository"
        )

    view = run_gh(path, "pr", "view", "--json", "url,state")
    if view.returncode == 0:
        try:
            data = json.loads(view.stdout)
        except json.JSONDecodeError:
            # успешный exit с мусором в stdout: создавать PR вслепую нельзя —
            # риск дубля; честная ошибка вместо догадки
            return ActionResult(
                action="open-pr",
                dir=str(path),
                ok=False,
                error="unexpected non-JSON output from `gh pr view`",
            )
        if data.get("state") == "OPEN":
            return ActionResult(
                action="open-pr",
                dir=str(path),
                ok=True,
                detail="pull request already open",
                pr_url=data.get("url"),
                pr_state="OPEN",
            )

    created = run_gh(path, "pr", "create", "--fill")
    if created.returncode != 0:
        return ActionResult(
            action="open-pr",
            dir=str(path),
            ok=False,
            error=created.stderr.strip() or "gh pr create failed",
        )
    url = created.stdout.strip().splitlines()[-1] if created.stdout.strip() else None
    if not url:
        return ActionResult(
            action="open-pr",
            dir=str(path),
            ok=False,
            error="`gh pr create` succeeded but returned no PR URL",
        )
    return ActionResult(
        action="open-pr",
        dir=str(path),
        ok=True,
        detail="pull request created",
        pr_url=url,
        pr_state="OPEN",
    )


def _sync_failure(path: Path, error: str) -> ActionResult:
    return ActionResult(
        action="post-merge-sync",
        dir=str(path),
        ok=False,
        local_sync="failed",
        error=error,
        local=local_status(path),
    )


def post_merge_sync(path: Path) -> ActionResult:
    """Return a clone to a freshly pulled default branch, destroying nothing.

    Every precondition that could cost work is a refusal, not a workaround:
    this never stashes, resets, force-switches or force-deletes. Runs after
    an already-merged, irreversible remote PR — the only thing still at
    risk here is the user's uncommitted local work.
    """
    if not is_git_repo(path):
        return ActionResult(
            action="post-merge-sync",
            dir=str(path),
            ok=True,
            local_sync="not_applicable",
            detail="no local clone to sync",
        )

    status = local_status(path)
    if status.dirty:
        return _sync_failure(path, "working tree is dirty; refusing to switch")
    if is_detached(path):
        return _sync_failure(path, "HEAD is detached; refusing to switch")

    set_head_auto(path)
    default = default_branch(path)
    if default is None:
        return _sync_failure(path, "cannot resolve the remote default branch")

    holder = worktree_holding(path, default)
    if holder is not None:
        return _sync_failure(
            path, f"branch {default} is checked out in another worktree: {holder}"
        )
    if not has_upstream(path, default):
        return _sync_failure(path, f"branch {default} has no upstream")

    try:
        fetch(path)
        switch_branch(path, default)
        pull_ff_only(path)
    except LocalGitError as err:
        return _sync_failure(path, str(err))

    removed: list[str] = []
    kept: list[str] = []
    for branch in merged_local_branches(path, default):
        try:
            delete_branch(path, branch)
            removed.append(branch)
        except LocalGitError:
            kept.append(branch)

    detail = f"synced {default}"
    if removed:
        detail += f"; deleted {len(removed)} merged branch(es)"
    if kept:
        detail += f"; kept {', '.join(kept)}"
    return ActionResult(
        action="post-merge-sync",
        dir=str(path),
        ok=True,
        local_sync="ok",
        detail=detail,
        branch=default,
        local=local_status(path),
    )
