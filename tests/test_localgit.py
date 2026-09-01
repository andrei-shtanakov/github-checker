import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from github_checker.localgit import (
    LocalGitError,
    delete_branch,
    fetch,
    has_upstream,
    is_detached,
    is_git_repo,
    local_status,
    merged_local_branches,
    pull_ff_only,
    switch_branch,
    worktree_holding,
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


def _init_bare_origin(path: Path) -> None:
    """Create a bare origin whose HEAD is pinned to main.

    Without an explicit ``-b`` the branch name comes from the runner's
    ``init.defaultBranch``. On a host where that is still ``master`` the
    clone of such an origin lands on an unborn ``master`` and a later
    ``push origin main`` fails with "src refspec main does not match any"
    (github-checker#35).
    """
    path.mkdir()
    _git(path, "init", "-q", "--bare", "-b", "main")


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
    _init_bare_origin(origin)
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
    _init_bare_origin(origin)
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
    _init_bare_origin(origin)
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


def _init(path: Path) -> None:
    """Init a repo directly at *path* (no subdir), branch `master`."""
    _git(path, "init", "-q", "-b", "master")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "f.txt").write_text("one\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-qm", "init")


def test_is_detached_reflects_head_state(tmp_path: Path) -> None:
    _init(tmp_path)
    assert is_detached(tmp_path) is False
    sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git(tmp_path, "checkout", "-q", sha)
    assert is_detached(tmp_path) is True


def test_has_upstream_is_false_without_a_remote(tmp_path: Path) -> None:
    _init(tmp_path)
    assert has_upstream(tmp_path, "master") is False


def test_worktree_holding_names_the_other_worktree(tmp_path: Path) -> None:
    main = tmp_path / "main"
    main.mkdir()
    _init(main)
    _git(main, "branch", "feature")
    other = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(other), "feature")
    holder = worktree_holding(main, "feature")
    assert holder is not None
    assert "wt" in holder
    assert worktree_holding(main, "master") is None


def test_worktree_holding_does_not_self_match_through_a_symlink(
    tmp_path: Path,
) -> None:
    # git worktree list --porcelain prints *resolved* paths. If the caller's
    # own path is unresolved (e.g. reached through a symlink, as /tmp is on
    # macOS), a naive string/Path comparison mismatches the repo's own entry
    # and makes it look like some other worktree holds its own branch.
    real = tmp_path / "real"
    real.mkdir()
    _init(real)
    _git(real, "branch", "feature")
    other = tmp_path / "wt"
    _git(real, "worktree", "add", "-q", str(other), "feature")
    link = tmp_path / "link"
    link.symlink_to(real)
    # Asking through the unresolved symlink about the repo's OWN checked-out
    # branch (master) must be None -- it must not report itself.
    assert worktree_holding(link, "master") is None
    # A genuinely different worktree must still be named correctly through
    # the same symlinked path, so the fix isn't just "always return None".
    holder = worktree_holding(link, "feature")
    assert holder is not None
    assert "wt" in holder


def test_switch_branch_moves_head(tmp_path: Path) -> None:
    _init(tmp_path)
    _git(tmp_path, "branch", "feature")
    switch_branch(tmp_path, "feature")
    current = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert current == "feature"


def test_merged_local_branches_excludes_the_base_and_unmerged_work(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "done")
    _git(tmp_path, "checkout", "-q", "master")
    _git(tmp_path, "checkout", "-q", "-b", "wip")
    (tmp_path / "g.txt").write_text("two\n")
    _git(tmp_path, "add", "g.txt")
    _git(tmp_path, "commit", "-qm", "wip")
    _git(tmp_path, "checkout", "-q", "master")
    merged = merged_local_branches(tmp_path, "master")
    assert "done" in merged
    assert "wip" not in merged
    assert "master" not in merged


def test_delete_branch_refuses_unmerged_work(tmp_path: Path) -> None:
    _init(tmp_path)
    _git(tmp_path, "checkout", "-q", "-b", "wip")
    (tmp_path / "g.txt").write_text("two\n")
    _git(tmp_path, "add", "g.txt")
    _git(tmp_path, "commit", "-qm", "wip")
    _git(tmp_path, "checkout", "-q", "master")
    with pytest.raises(LocalGitError):
        delete_branch(tmp_path, "wip")
