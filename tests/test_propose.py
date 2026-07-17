"""propose-pr: scoped content-PR from an isolated worktree (spec 2026-07-17)."""

import subprocess
from pathlib import Path

import pytest

from github_checker import actions
from github_checker.propose import (
    Edit,
    ProposeError,
    normalize_repo_path,
    parse_edits,
    parse_if_match,
    propose_pr,
)


def _content(tmp_path: Path, name: str = "c.txt", text: str = "new\n") -> Path:
    f = tmp_path / name
    f.write_text(text)
    return f


def test_parse_edits_splits_on_first_equals(tmp_path: Path) -> None:
    f = _content(tmp_path, "with=eq.txt")
    edits = parse_edits([f"docs/a.md={f}"])
    assert edits == [Edit(repo_path="docs/a.md", content_file=f)]


def test_parse_edits_rejects_missing_equals(tmp_path: Path) -> None:
    with pytest.raises(ProposeError, match="expected <repo-path>=<content-file>"):
        parse_edits(["no-separator"])


def test_parse_edits_rejects_unreadable_or_irregular_content(tmp_path: Path) -> None:
    with pytest.raises(ProposeError, match="not a readable regular file"):
        parse_edits([f"a.txt={tmp_path / 'absent'}"])
    d = tmp_path / "dir"
    d.mkdir()
    with pytest.raises(ProposeError, match="not a readable regular file"):
        parse_edits([f"a.txt={d}"])


def test_parse_edits_rejects_unreadable_permissions(tmp_path: Path) -> None:
    import os

    f = _content(tmp_path)
    f.chmod(0)
    try:
        if os.access(f, os.R_OK):  # e.g. running as root — cannot exercise this
            pytest.skip("filesystem grants read despite chmod 0")
        with pytest.raises(ProposeError, match="not a readable regular file"):
            parse_edits([f"a.txt={f}"])
    finally:
        f.chmod(0o600)


def test_parse_edits_duplicate_after_normalization(tmp_path: Path) -> None:
    f = _content(tmp_path)
    with pytest.raises(ProposeError, match="duplicate repo path"):
        parse_edits([f"a/b.txt={f}", f"./a//b.txt={f}"])


@pytest.mark.parametrize(
    "bad",
    [
        "/abs.txt",
        "../up.txt",
        "a/../b",  # normpath would collapse this to "b" — must reject RAW
        "a/../../up.txt",
        ".git/hooks/x",
        "a/.git/x",
        "",
    ],
)
def test_normalize_repo_path_rejects_escapes(bad: str) -> None:
    with pytest.raises(ProposeError):
        normalize_repo_path(bad)


def test_normalize_repo_path_normalizes() -> None:
    assert normalize_repo_path("./a//b.txt") == "a/b.txt"


def test_parse_if_match(tmp_path: Path) -> None:
    got = parse_if_match(["project.yaml=" + "a" * 64])
    assert got == {"project.yaml": "a" * 64}
    # uppercase hex is normalized, not rejected
    assert parse_if_match(["p.yaml=" + "A" * 64]) == {"p.yaml": "a" * 64}
    with pytest.raises(ProposeError, match="expected <repo-path>=<sha256>"):
        parse_if_match(["project.yaml"])
    with pytest.raises(ProposeError, match="not a sha256"):
        parse_if_match(["project.yaml=nothex"])
    with pytest.raises(ProposeError, match="duplicate repo path"):
        parse_if_match(["a/b=" + "a" * 64, "./a//b=" + "b" * 64])


def _git(path: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def _make_pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "project.yaml").write_text("spec_runner:\n  max_retries: 3\n")
    _git(seed, "add", "project.yaml")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)],
        check=True,
        capture_output=True,
    )
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    return origin, seed, clone


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _gh_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        actions,
        "_gh",
        lambda path, *args: _FakeProc(0, stdout="https://github.com/o/r/pull/7\n"),
    )


def test_happy_path_lands_branch_in_origin_live_tree_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    origin, _, clone = _make_pair(tmp_path)
    _gh_ok(monkeypatch)
    live_before = (clone / "project.yaml").read_bytes()
    content = tmp_path / "new.yaml"
    content.write_text("spec_runner:\n  max_retries: 9\n")

    result = propose_pr(
        clone, message="bump retries", edit_args=[f"project.yaml={content}"]
    )

    assert result.ok, result.error
    assert result.pr_url == "https://github.com/o/r/pull/7"
    assert result.base_branch == "main"
    assert result.branch and result.branch.startswith("propose/")
    assert result.changed_paths == ["project.yaml"]
    assert result.commit_sha
    # the edit is on the remote branch
    blob = _git(origin, "show", f"{result.branch}:project.yaml")
    assert "max_retries: 9" in blob
    # the live working tree is byte-for-byte untouched
    assert (clone / "project.yaml").read_bytes() == live_before
    # temp worktree and local branch are cleaned up
    assert "propose/" not in _git(clone, "branch", "--list", "propose/*")
    assert result.branch not in _git(clone, "worktree", "list")


def test_dirty_live_checkout_same_file_still_bases_on_default(
    tmp_path: Path, monkeypatch
) -> None:
    origin, _, clone = _make_pair(tmp_path)
    _gh_ok(monkeypatch)
    (clone / "project.yaml").write_text("OPERATOR LOCAL WIP\n")
    content = tmp_path / "new.yaml"
    content.write_text("spec_runner:\n  max_retries: 9\n")

    result = propose_pr(clone, message="bump", edit_args=[f"project.yaml={content}"])

    assert result.ok, result.error
    blob = _git(origin, "show", f"{result.branch}:project.yaml")
    assert "OPERATOR LOCAL WIP" not in blob  # based on origin/main, not live tree
    assert (clone / "project.yaml").read_text() == "OPERATOR LOCAL WIP\n"


def test_multiple_edits_one_commit(tmp_path: Path, monkeypatch) -> None:
    origin, _, clone = _make_pair(tmp_path)
    _gh_ok(monkeypatch)
    a = tmp_path / "a.txt"
    a.write_text("A\n")
    b = tmp_path / "b.txt"
    b.write_text("B\n")

    result = propose_pr(
        clone,
        message="two files",
        edit_args=[f"docs/a.txt={a}", f"b.txt={b}"],
    )

    assert result.ok, result.error
    assert result.changed_paths == ["b.txt", "docs/a.txt"]  # sorted
    count = _git(origin, "rev-list", "--count", f"main..{result.branch}")
    assert count == "1"


def test_noop_returns_structural_marker(tmp_path: Path, monkeypatch) -> None:
    _, _, clone = _make_pair(tmp_path)
    _gh_ok(monkeypatch)
    same = tmp_path / "same.yaml"
    same.write_text("spec_runner:\n  max_retries: 3\n")  # identical to origin/main

    result = propose_pr(clone, message="noop", edit_args=[f"project.yaml={same}"])

    assert not result.ok
    assert result.detail == "no-op"
    assert result.error is not None
    # nothing was pushed
    assert result.branch is None or "propose" not in _git(
        clone, "ls-remote", "--heads", "origin", "propose/*"
    )


def test_parse_error_degrades_to_result(tmp_path: Path) -> None:
    _, _, clone = _make_pair(tmp_path)
    result = propose_pr(clone, message="x", edit_args=["no-separator"])
    assert not result.ok
    assert "expected" in (result.error or "")


def test_not_a_repo(tmp_path: Path) -> None:
    result = propose_pr(tmp_path, message="x", edit_args=[])
    assert not result.ok
