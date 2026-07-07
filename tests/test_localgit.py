import subprocess
from pathlib import Path

import pytest

from github_checker.localgit import (
    LocalGitError,
    fetch,
    local_status,
    pull_ff_only,
)


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "f.txt").write_text("one\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "init")


def test_local_status_missing_path(tmp_path: Path) -> None:
    status = local_status(tmp_path / "nope")
    assert status.error is not None
    assert status.branch is None


def test_local_status_non_git_dir(tmp_path: Path) -> None:
    status = local_status(tmp_path)
    assert status.error is not None


def test_local_status_no_upstream(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    status = local_status(repo)
    assert status.error is None
    assert status.branch == "main"
    assert status.ahead is None
    assert status.behind is None
    assert status.dirty is False


def test_local_status_dirty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "untracked.txt").write_text("x\n")
    assert local_status(repo).dirty is True


def test_local_status_ahead_of_upstream(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare")
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    (repo / "f.txt").write_text("two\n")
    _git(repo, "commit", "-q", "-am", "second")
    status = local_status(repo)
    assert status.ahead == 1
    assert status.behind == 0


def test_fetch_unreachable_remote_raises(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", str(tmp_path / "nonexistent.git"))
    with pytest.raises(LocalGitError):
        fetch(repo)


def test_pull_ff_only_succeeds(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare")
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    fetch(repo)  # no error now that a remote exists
    pull_ff_only(repo)  # already up to date -> ff-only is a no-op, no error
