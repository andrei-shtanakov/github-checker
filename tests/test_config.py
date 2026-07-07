from pathlib import Path

import pytest
from pydantic import ValidationError

from github_checker.config import (
    add_repo,
    default_config_path,
    load_config,
    remove_repo,
    resolve_config_path,
    save_config,
    set_path,
)
from github_checker.models import Config, RepoRef


def test_default_config_path_respects_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert default_config_path() == tmp_path / "github-checker" / "repos.toml"


def test_resolve_config_path_explicit_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "custom.toml"
    assert resolve_config_path(explicit) == explicit


def test_resolve_config_path_migrates_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    workdir = tmp_path / "project"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    save_config(workdir / "repos.toml", Config(repos=["o/legacy"]))
    resolved = resolve_config_path(None)
    assert resolved == tmp_path / "xdg" / "github-checker" / "repos.toml"
    assert [r.name for r in load_config(resolved).repos] == ["o/legacy"]
    assert not (workdir / "repos.toml").exists()


def test_resolve_config_path_existing_target_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    workdir = tmp_path / "project"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    target = tmp_path / "xdg" / "github-checker" / "repos.toml"
    save_config(target, Config(repos=["o/mine"]))
    save_config(workdir / "repos.toml", Config(repos=["o/legacy"]))
    resolved = resolve_config_path(None)
    assert [r.name for r in load_config(resolved).repos] == ["o/mine"]


def test_load_missing_creates_empty(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    config = load_config(path)
    assert config.repos == []
    assert path.exists()


def test_load_missing_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "repos.toml"
    config = load_config(path)
    assert config.repos == []
    assert path.exists()


def test_save_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=["o/r"], refresh_seconds=30))
    loaded = load_config(path)
    assert [r.name for r in loaded.repos] == ["o/r"]
    assert loaded.repos[0].path is None
    assert loaded.refresh_seconds == 30


def test_save_load_roundtrip_with_path(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    ref = RepoRef(name="o/r", path=Path("/tmp/o-r"))
    save_config(path, Config(repos=[ref]))
    loaded = load_config(path)
    assert loaded.repos == [ref]


def test_add_repo(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=["o/r"]))
    config = add_repo(path, "o/two")
    assert [r.name for r in config.repos] == ["o/r", "o/two"]
    assert [r.name for r in load_config(path).repos] == ["o/r", "o/two"]


def test_add_repo_preserves_existing_path(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=[RepoRef(name="o/r", path=Path("/tmp/o-r"))]))
    config = add_repo(path, "o/two")
    assert config.repos[0].path == Path("/tmp/o-r")


def test_add_repo_duplicate_noop(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=["o/r"]))
    assert [r.name for r in add_repo(path, "o/r").repos] == ["o/r"]


def test_add_repo_invalid_raises(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config())
    with pytest.raises(ValidationError):
        add_repo(path, "garbage")


def test_remove_repo(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=["o/r", "o/two"]))
    config = remove_repo(path, "o/r")
    assert [r.name for r in config.repos] == ["o/two"]
    assert [r.name for r in load_config(path).repos] == ["o/two"]


def test_set_path_sets_and_clears(tmp_path: Path) -> None:
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    clone = tmp_path / "clone"

    updated = set_path(config_path, "o/r", clone)
    assert updated.repos[0].path == clone
    assert load_config(config_path).repos[0].path == clone

    changed = tmp_path / "other"
    set_path(config_path, "o/r", changed)
    assert load_config(config_path).repos[0].path == changed

    set_path(config_path, "o/r", None)
    assert load_config(config_path).repos[0].path is None


def test_set_path_unknown_name_is_noop(tmp_path: Path) -> None:
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    updated = set_path(config_path, "o/missing", tmp_path / "x")
    assert [r.name for r in updated.repos] == ["o/r"]
    assert updated.repos[0].path is None
