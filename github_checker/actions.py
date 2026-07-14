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


def _gh(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args],
        cwd=path,
        capture_output=True,
        text=True,
        timeout=60,
    )


def pull(path: Path) -> ActionResult:
    """`git pull --ff-only` in *path*; refuses non-repos, reports final state."""
    if not is_git_repo(path):
        return ActionResult(
            action="pull", dir=str(path), ok=False, error="not a git repository"
        )
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
    return ActionResult(
        action="pull",
        dir=str(path),
        ok=True,
        detail="fast-forwarded",
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
            data = {}
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
    return ActionResult(
        action="open-pr",
        dir=str(path),
        ok=True,
        detail="pull request created",
        pr_url=url,
        pr_state="OPEN",
    )
