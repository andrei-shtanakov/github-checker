"""Local git clone status and safe sync operations."""

import subprocess
from pathlib import Path

from github_checker.models import LocalStatus


class LocalGitError(Exception):
    """A failed local git operation."""


def _git(path: Path, *args: str) -> str:
    """Run `git -C path *args`, returning stripped stdout or raising."""
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise LocalGitError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def local_status(path: Path) -> LocalStatus:
    """Describe a clone relative to its upstream; never raises."""
    if not path.exists():
        return LocalStatus(
            branch=None,
            ahead=None,
            behind=None,
            dirty=False,
            error="path not found",
        )
    try:
        branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
        dirty = bool(_git(path, "status", "--porcelain"))
        ahead: int | None = None
        behind: int | None = None
        try:
            counts = _git(
                path,
                "rev-list",
                "--left-right",
                "--count",
                "@{upstream}...HEAD",
            )
            behind_str, ahead_str = counts.split()
            behind, ahead = int(behind_str), int(ahead_str)
        except LocalGitError:
            pass  # no upstream configured
        return LocalStatus(
            branch=branch, ahead=ahead, behind=behind, dirty=dirty, error=None
        )
    except LocalGitError as err:
        return LocalStatus(
            branch=None, ahead=None, behind=None, dirty=False, error=str(err)
        )


def fetch(path: Path) -> None:
    """Run `git fetch --prune`; raises LocalGitError on failure."""
    _git(path, "fetch", "--prune")


def pull_ff_only(path: Path) -> None:
    """Run `git pull --ff-only`; raises LocalGitError on divergence/failure."""
    _git(path, "pull", "--ff-only")
