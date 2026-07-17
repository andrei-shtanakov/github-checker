import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from github_checker.localgit import (
    LocalGitError,
    fetch,
    is_git_repo,
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


def test_is_git_repo_true_for_clone(tmp_path: Path) -> None:
    repo = tmp_path / "clone"
    _init_repo(repo)
    assert is_git_repo(repo) is True


def test_is_git_repo_false_for_plain_dir(tmp_path: Path) -> None:
    assert is_git_repo(tmp_path) is False


def test_is_git_repo_false_for_missing_path(tmp_path: Path) -> None:
    assert is_git_repo(tmp_path / "nope") is False


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


def _pair(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Callable[..., None]]:
    """Build origin (bare), seed (local), clone (remote clone), and git runner."""

    def g(path: Path, *args: str) -> None:
        subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    origin = tmp_path / "origin.git"
    origin.mkdir()
    g(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    g(seed, "init", "-q", "-b", "main")
    g(seed, "config", "user.email", "t@example.com")
    g(seed, "config", "user.name", "t")
    (seed / "f.txt").write_bytes(b"one\r\n")  # CRLF on purpose (raw-bytes test)
    g(seed, "add", "f.txt")
    g(seed, "commit", "-q", "-m", "init")
    g(seed, "remote", "add", "origin", str(origin))
    g(seed, "push", "-q", "-u", "origin", "main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)],
        check=True,
        capture_output=True,
    )
    return origin, seed, clone, g


def test_default_branch_from_fresh_clone(tmp_path: Path) -> None:
    from github_checker.localgit import default_branch

    _, _, clone, _ = _pair(tmp_path)
    assert default_branch(clone) == "main"


def test_default_branch_none_when_head_unset(tmp_path: Path) -> None:
    from github_checker.localgit import default_branch

    _, _, clone, g = _pair(tmp_path)
    g(clone, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    assert default_branch(clone) is None


def test_set_head_auto_refreshes_stale_head(tmp_path: Path) -> None:
    from github_checker.localgit import default_branch, set_head_auto

    origin, seed, clone, g = _pair(tmp_path)
    # remote's default branch changes to new-main AFTER the clone
    g(seed, "switch", "-q", "-c", "new-main")
    g(seed, "push", "-q", "-u", "origin", "new-main")
    g(origin, "symbolic-ref", "HEAD", "refs/heads/new-main")
    assert default_branch(clone) == "main"  # stale
    fetch(clone)
    set_head_auto(clone)
    assert default_branch(clone) == "new-main"  # refreshed


def test_blob_bytes_raw_and_absent(tmp_path: Path) -> None:
    from github_checker.localgit import blob_bytes

    _, _, clone, _ = _pair(tmp_path)
    assert blob_bytes(clone, "origin/main", "f.txt") == b"one\r\n"  # raw CRLF
    assert blob_bytes(clone, "origin/main", "missing.txt") is None


def test_blob_bytes_invalid_ref_raises(tmp_path: Path) -> None:
    from github_checker.localgit import LocalGitError, blob_bytes

    _, _, clone, _ = _pair(tmp_path)
    with pytest.raises(LocalGitError):
        blob_bytes(clone, "no-such-ref-xyz", "f.txt")


def test_blob_bytes_broken_repo_raises(tmp_path: Path) -> None:
    from github_checker.localgit import LocalGitError, blob_bytes

    with pytest.raises(LocalGitError):
        blob_bytes(tmp_path / "not-a-repo", "origin/main", "f.txt")
