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


def test_git_binary_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    def _boom(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr("github_checker.localgit.subprocess.run", _boom)
    status = local_status(repo)  # must not raise
    assert status.error is not None
    assert status.branch is None
    with pytest.raises(LocalGitError):
        fetch(repo)


def test_pull_ff_only_divergence_raises(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare")
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    # Second clone advances origin with a commit repo does not have.
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _git(other, "config", "user.email", "o@example.com")
    _git(other, "config", "user.name", "o")
    (other / "g.txt").write_text("from other\n")
    _git(other, "add", "g.txt")
    _git(other, "commit", "-q", "-m", "other")
    _git(other, "push", "-q", "origin", "main")
    # repo makes its own diverging commit, then fetches -> not a fast-forward.
    (repo / "f.txt").write_text("local change\n")
    _git(repo, "commit", "-q", "-am", "local")
    fetch(repo)
    with pytest.raises(LocalGitError):
        pull_ff_only(repo)
