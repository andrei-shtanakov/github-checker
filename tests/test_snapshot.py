import subprocess
from pathlib import Path

import pytest

import github_checker.snapshot as snap
from github_checker.models import RepoState
from github_checker.snapshot import (
    build_snapshot,
    discover,
    parse_github_remote,
)


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("git@github.com:owner/repo", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo/", "owner/repo"),
        ("ssh://git@github.com/owner/repo.git", "owner/repo"),
        ("git@git.epam.com:owner/repo.git", None),
        ("https://gitlab.com/owner/repo.git", None),
        ("not a url", None),
    ],
)
def test_parse_github_remote(url: str, slug: str | None) -> None:
    assert parse_github_remote(url) == slug


def _init_repo(path: Path, remote: str | None = None) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    if remote is not None:
        subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)


def test_discover_finds_only_git_dirs(tmp_path: Path) -> None:
    _init_repo(tmp_path / "alpha")
    _init_repo(tmp_path / "beta")
    (tmp_path / "not-a-repo").mkdir()
    (tmp_path / "loose-file.md").write_text("hi", encoding="utf-8")
    assert [p.name for p in discover(tmp_path)] == ["alpha", "beta"]


@pytest.mark.anyio
async def test_build_snapshot_local_only(tmp_path: Path) -> None:
    _init_repo(tmp_path / "alpha", remote="git@github.com:o/alpha.git")
    _init_repo(tmp_path / "beta")  # без remote
    snapshot = await build_snapshot(tmp_path, include_github=False)
    assert snapshot.gh_error == "skipped (--local-only)"
    assert snapshot.host
    assert [r.dir for r in snapshot.repos] == ["alpha", "beta"]
    alpha, beta = snapshot.repos
    assert alpha.remote == "o/alpha"
    assert beta.remote is None
    assert all(r.github is None for r in snapshot.repos)
    # свежий init: чисто, upstream нет
    assert alpha.local.dirty is False
    assert alpha.local.ahead is None


@pytest.mark.anyio
async def test_build_snapshot_gh_unavailable_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path / "alpha", remote="git@github.com:o/alpha.git")
    monkeypatch.setattr(snap, "gh_ready", lambda: "gh не авторизован")
    snapshot = await build_snapshot(tmp_path)
    assert snapshot.gh_error == "gh не авторизован"
    assert snapshot.repos[0].github is None


@pytest.mark.anyio
async def test_build_snapshot_maps_github_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path / "alpha", remote="git@github.com:o/alpha.git")
    _init_repo(tmp_path / "beta")  # remote нет — в fetch_all не попадает

    async def fake_fetch_all(refs: list) -> list[RepoState]:
        assert [r.name for r in refs] == ["o/alpha"]
        return [RepoState(name="o/alpha")]

    monkeypatch.setattr(snap, "gh_ready", lambda: None)
    monkeypatch.setattr(snap, "fetch_all", fake_fetch_all)
    snapshot = await build_snapshot(tmp_path)
    assert snapshot.gh_error is None
    alpha, beta = snapshot.repos
    assert alpha.github is not None and alpha.github.name == "o/alpha"
    assert beta.github is None
