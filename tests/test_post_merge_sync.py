"""post-merge-sync never destroys local work; refusal beats cleverness."""

import subprocess
from pathlib import Path

from github_checker.actions import post_merge_sync


def _run(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )


def make_pair(tmp_path: Path) -> tuple[Path, Path]:
    """An origin repo plus a clone whose origin/HEAD is resolvable."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _run(seed, "init", "-q", "-b", "master")
    _run(seed, "config", "user.email", "t@example.com")
    _run(seed, "config", "user.name", "t")
    (seed / "f.txt").write_text("one\n")
    _run(seed, "add", "f.txt")
    _run(seed, "commit", "-qm", "init")
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(origin)],
        check=True,
        capture_output=True,
    )
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True
    )
    _run(clone, "config", "user.email", "t@example.com")
    _run(clone, "config", "user.name", "t")
    return origin, clone


def test_missing_clone_is_not_applicable_not_an_error(tmp_path: Path) -> None:
    result = post_merge_sync(tmp_path / "nope")
    assert result.ok is True
    assert result.local_sync == "not_applicable"


def test_clean_clone_syncs_and_reports_ok(tmp_path: Path) -> None:
    _, clone = make_pair(tmp_path)
    result = post_merge_sync(clone)
    assert result.ok is True
    assert result.local_sync == "ok"
    assert result.branch == "master"


def test_dirty_tree_is_refused_and_changes_survive(tmp_path: Path) -> None:
    _, clone = make_pair(tmp_path)
    (clone / "f.txt").write_text("local edit\n")
    result = post_merge_sync(clone)
    assert result.ok is False
    assert result.local_sync == "failed"
    assert "dirty" in (result.error or "").lower()
    assert (clone / "f.txt").read_text() == "local edit\n"


def test_untracked_file_also_refuses(tmp_path: Path) -> None:
    _, clone = make_pair(tmp_path)
    (clone / "scratch.txt").write_text("notes\n")
    result = post_merge_sync(clone)
    assert result.ok is False
    assert (clone / "scratch.txt").exists()


def test_detached_head_is_refused(tmp_path: Path) -> None:
    _, clone = make_pair(tmp_path)
    sha = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _run(clone, "checkout", "-q", sha)
    result = post_merge_sync(clone)
    assert result.ok is False
    assert "detached" in (result.error or "").lower()


def test_default_branch_held_by_another_worktree_is_refused(tmp_path: Path) -> None:
    _, clone = make_pair(tmp_path)
    _run(clone, "checkout", "-q", "-b", "side")
    other = tmp_path / "wt"
    _run(clone, "worktree", "add", "-q", str(other), "master")
    result = post_merge_sync(clone)
    assert result.ok is False
    assert "worktree" in (result.error or "").lower()


def test_merged_branch_is_deleted_and_unmerged_work_is_kept(tmp_path: Path) -> None:
    _, clone = make_pair(tmp_path)
    _run(clone, "branch", "already-merged")
    _run(clone, "checkout", "-q", "-b", "keep-me")
    (clone / "g.txt").write_text("work\n")
    _run(clone, "add", "g.txt")
    _run(clone, "commit", "-qm", "wip")
    _run(clone, "checkout", "-q", "master")
    result = post_merge_sync(clone)
    assert result.ok is True
    branches = subprocess.run(
        ["git", "-C", str(clone), "branch", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert "already-merged" not in branches
    assert "keep-me" in branches
