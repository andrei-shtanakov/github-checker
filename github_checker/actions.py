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
from github_checker.models import IssueRef, LocalStatus, PrDetail


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
    matches: list[IssueRef] | None = None
    malformed: list[IssueRef] | None = None
    created: bool | None = None
    issue: IssueRef | None = None


# --- contracts/actions/v1: which fields each verb is *about* -----------------
#
# Wire semantics the schema freezes:
#   field absent            -> the verb has no such concept
#   field present, null     -> applicable, value unknown
#   field present, false    -> applicable, definitely negative
#
# The envelope carries 20 optional fields, so serialising the model as-is
# emits all of them for every verb and `pull.matches = null` becomes
# indistinguishable from `issue-lookup.matches = null` — one meaning "no such
# concept", the other "the inbox was not read exhaustively". That is the
# conflation this map exists to remove.
#
# Serialisation itself is `exclude_unset=True`: the model is the single
# source of truth, and what a call site explicitly set is what ships. This
# map is the *assertion* over that — `result_for` fills an action's whole set
# with explicit `None` so no site can forget one, and `_emit` checks the
# resulting key set before printing. 32 of the 37 hand-written constructions
# omitted at least one applicable field, which is why the filling is done in
# one place rather than trusted to each caller.
ENVELOPE_FIELDS = ("action", "dir", "ok")

ACTION_FIELDS: dict[str, frozenset[str]] = {
    "pull": frozenset({"error", "detail", "local"}),
    "open-pr": frozenset({"error", "detail", "pr_url", "pr_state"}),
    "propose-pr": frozenset(
        {
            "error",
            "detail",
            "pr_url",
            "pr_state",
            "branch",
            "base_branch",
            "commit_sha",
            "changed_paths",
        }
    ),
    "pr-detail": frozenset({"error", "pr_detail"}),
    "merge": frozenset(
        {
            "error",
            "detail",
            "pr_url",
            "merged",
            "local_sync",
            "gate_failed",
            "pr_detail",
        }
    ),
    "post-merge-sync": frozenset({"error", "detail", "local", "branch", "local_sync"}),
    "issue-lookup": frozenset({"error", "matches", "malformed"}),
    "issue-create": frozenset(
        {"error", "detail", "created", "issue", "matches", "malformed"}
    ),
}


def wire_fields(action: str) -> frozenset[str]:
    """Fields this action is about, beyond the always-present envelope.

    An action outside the map — an unknown verb, or a refusal raised before
    one was identified — is about nothing but its own failure, so only
    `error` accompanies the envelope. That is the CLI-parse-error variant.
    """
    return ACTION_FIELDS.get(action, frozenset({"error"}))


def contracted_keys(action: str) -> set[str]:
    """The exact JSON key set this action must serialise to."""
    return set(ENVELOPE_FIELDS) | wire_fields(action)


def result_for(
    action: str, path: object, *, ok: bool, **fields: object
) -> ActionResult:
    """An ActionResult already carrying every field this action is about.

    Serialisation uses `exclude_unset=True`, so a field a call site forgot
    would vanish from the wire — and under this contract an absent field
    means "the verb has no such concept", not "unknown". Filling the
    action's whole set with explicit `None` first makes the shape a
    property of the verb rather than of what somebody remembered to pass:
    32 of the 37 hand-written constructions omitted at least one.

    Values passed by the caller win; everything else stays an explicit
    `None`, which is exactly the "applicable, unknown" the contract wants.
    """
    applicable = wire_fields(action)
    foreign = sorted(set(fields) - applicable)
    if foreign:
        # Not silently dropped: a field outside this action's shape means the
        # caller believes the verb reports something it does not, and that
        # belief has to surface where it was formed.
        raise ValueError(f"{action} has no concept of {foreign}")
    base: dict[str, object] = dict.fromkeys(applicable)
    base.update(fields)
    return ActionResult(action=action, dir=str(path), ok=ok, **base)  # type: ignore[arg-type]


def pull(path: Path) -> ActionResult:
    """`git pull --ff-only` in *path*; refuses non-repos, reports final state."""
    if not is_git_repo(path):
        return result_for("pull", path, ok=False, error="not a git repository")
    before = head_rev(path)
    try:
        pull_ff_only(path)
    except LocalGitError as err:
        return result_for(
            "pull", path, ok=False, error=str(err), local=local_status(path)
        )
    detail = "already up to date" if head_rev(path) == before else "fast-forwarded"
    return result_for("pull", path, ok=True, detail=detail, local=local_status(path))


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
    return result_for(
        "post-merge-sync",
        path,
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
        return result_for(
            "post-merge-sync",
            path,
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
    cleanup_note = ""
    try:
        candidates = merged_local_branches(path, default)
    except LocalGitError as err:
        # Listing failed (e.g. a user-side `branch.sort` misconfiguration) —
        # the sync itself already succeeded, so this stays ok=True; branch
        # cleanup is cosmetic, not part of the contract.
        candidates = []
        cleanup_note = f"; branch cleanup skipped: {err}"

    for branch in candidates:
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
    detail += cleanup_note
    return result_for(
        "post-merge-sync",
        path,
        ok=True,
        local_sync="ok",
        detail=detail,
        branch=default,
        local=local_status(path),
    )
