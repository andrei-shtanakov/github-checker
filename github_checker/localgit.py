"""Local git clone status and safe sync operations."""

import subprocess
from pathlib import Path

from github_checker.models import LocalStatus


class LocalGitError(Exception):
    """A failed local git operation."""


def _git(path: Path, *args: str) -> str:
    """Run `git -C path *args`, returning stripped stdout or raising."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as err:
        raise LocalGitError(str(err)) from err
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
        except (LocalGitError, ValueError):
            pass  # no upstream configured or unexpected output
        return LocalStatus(
            branch=branch, ahead=ahead, behind=behind, dirty=dirty, error=None
        )
    except LocalGitError as err:
        return LocalStatus(
            branch=None, ahead=None, behind=None, dirty=False, error=str(err)
        )


def is_git_repo(path: Path) -> bool:
    """True if *path* exists and is a git work tree (clone/worktree/submodule)."""
    if not path.exists():
        return False
    try:
        _git(path, "rev-parse", "--git-dir")
    except LocalGitError:
        return False
    return True


def remote_url(path: Path, name: str = "origin") -> str | None:
    """URL of a remote, or None if the remote is not configured."""
    try:
        return _git(path, "remote", "get-url", name)
    except LocalGitError:
        return None


def head_rev(path: Path) -> str | None:
    """Commit hash of HEAD, or None if it cannot be resolved."""
    try:
        return _git(path, "rev-parse", "HEAD")
    except LocalGitError:
        return None


def fetch(path: Path) -> None:
    """Run `git fetch --prune`; raises LocalGitError on failure."""
    _git(path, "fetch", "--prune")


def pull_ff_only(path: Path) -> None:
    """Run `git pull --ff-only`; raises LocalGitError on divergence/failure."""
    _git(path, "pull", "--ff-only")


def set_head_auto(path: Path) -> None:
    """Best-effort `git remote set-head origin -a`; never raises.

    origin/HEAD can be stale when the remote's default branch changed
    after clone — a plain fetch does not update it.
    """
    try:
        _git(path, "remote", "set-head", "origin", "-a")
    except LocalGitError:
        pass


def default_branch(path: Path) -> str | None:
    """Default branch per refs/remotes/origin/HEAD, or None if unset."""
    try:
        ref = _git(path, "symbolic-ref", "refs/remotes/origin/HEAD")
    except LocalGitError:
        return None
    prefix = "refs/remotes/origin/"
    return ref.removeprefix(prefix) if ref.startswith(prefix) else None


def blob_bytes(path: Path, ref: str, repo_path: str) -> bytes | None:
    """Raw bytes of `<ref>:<repo_path>` (no smudge filters); None if absent."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "cat-file", "blob", f"{ref}:{repo_path}"],
            capture_output=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as err:
        raise LocalGitError(str(err)) from err
    if result.returncode != 0:
        return None
    return result.stdout
