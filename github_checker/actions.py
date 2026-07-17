"""Headless whitelist actions: CLI twins of the TUI keys `s`/`S` plus PR creation.

Consumers are programs (dispatcher's action endpoints), so every action prints
one JSON `ActionResult`. The whitelist is deliberately tiny and safe:
`pull` is fast-forward-only by construction; `open-pr` never pushes — it only
creates (or reports) a pull request for an already-pushed branch.
"""

import json
import subprocess
from pathlib import Path

from pydantic import BaseModel

from github_checker.localgit import (
    LocalGitError,
    head_rev,
    is_git_repo,
    local_status,
    pull_ff_only,
)
from github_checker.models import LocalStatus


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


def _gh(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run gh; never raises — a missing binary or timeout becomes a failed result."""
    try:
        return subprocess.run(
            ["gh", *args],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as err:
        return subprocess.CompletedProcess(
            ["gh", *args], returncode=127, stdout="", stderr=str(err)
        )


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

    view = _gh(path, "pr", "view", "--json", "url,state")
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

    created = _gh(path, "pr", "create", "--fill")
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
