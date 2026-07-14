"""Headless whitelist actions (H-2): pull is ff-only, open-pr is idempotent."""

import json
import subprocess
from pathlib import Path

from github_checker import actions
from github_checker.actions import open_pr, pull


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _make_pair(tmp_path: Path) -> tuple[Path, Path]:
    """A bare origin plus a clone tracking it, with one commit."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "f.txt").write_text("one\n")
    _git(seed, "add", "f.txt")
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
    return seed, clone


def test_pull_not_a_repo(tmp_path: Path) -> None:
    result = pull(tmp_path)
    assert not result.ok
    assert result.action == "pull"
    assert result.error is not None


def test_pull_fast_forwards_behind_clone(tmp_path: Path) -> None:
    seed, clone = _make_pair(tmp_path)
    (seed / "f.txt").write_text("two\n")
    _git(seed, "commit", "-qam", "update")
    _git(seed, "push", "-q")

    result = pull(clone)
    assert result.ok, result.error
    assert result.local is not None
    assert result.local.behind == 0
    assert (clone / "f.txt").read_text() == "two\n"


def test_pull_refuses_divergence(tmp_path: Path) -> None:
    seed, clone = _make_pair(tmp_path)
    (seed / "f.txt").write_text("theirs\n")
    _git(seed, "commit", "-qam", "theirs")
    _git(seed, "push", "-q")
    (clone / "f.txt").write_text("ours\n")
    _git(clone, "commit", "-qam", "ours")

    result = pull(clone)
    assert not result.ok
    assert result.error is not None
    # диверженция не тронута: локальный коммит на месте
    assert (clone / "f.txt").read_text() == "ours\n"


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_open_pr_reports_existing(tmp_path: Path, monkeypatch) -> None:
    _, clone = _make_pair(tmp_path)
    view = json.dumps({"url": "https://github.com/o/r/pull/5", "state": "OPEN"})
    monkeypatch.setattr(
        actions, "_gh", lambda path, *args: _FakeProc(0, stdout=view)
    )
    result = open_pr(clone)
    assert result.ok
    assert result.detail == "pull request already open"
    assert result.pr_url == "https://github.com/o/r/pull/5"


def test_open_pr_creates_when_none(tmp_path: Path, monkeypatch) -> None:
    _, clone = _make_pair(tmp_path)

    def fake_gh(path: Path, *args: str) -> _FakeProc:
        if args[:2] == ("pr", "view"):
            return _FakeProc(1, stderr="no pull requests found")
        assert args[:3] == ("pr", "create", "--fill")
        return _FakeProc(0, stdout="https://github.com/o/r/pull/6\n")

    monkeypatch.setattr(actions, "_gh", fake_gh)
    result = open_pr(clone)
    assert result.ok
    assert result.pr_url == "https://github.com/o/r/pull/6"
    assert result.pr_state == "OPEN"


def test_open_pr_surfaces_gh_error(tmp_path: Path, monkeypatch) -> None:
    _, clone = _make_pair(tmp_path)

    def fake_gh(path: Path, *args: str) -> _FakeProc:
        if args[:2] == ("pr", "view"):
            return _FakeProc(1)
        return _FakeProc(1, stderr="must first push the current branch")

    monkeypatch.setattr(actions, "_gh", fake_gh)
    result = open_pr(clone)
    assert not result.ok
    assert "push" in (result.error or "")
