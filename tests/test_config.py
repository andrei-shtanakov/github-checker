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
)
from github_checker.models import Config


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
    assert load_config(resolved).repos == ["o/legacy"]
    assert not (workdir / "repos.toml").exists()  # legacy removed after move


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
    assert load_config(resolved).repos == ["o/mine"]


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
    assert loaded.repos == ["o/r"]
    assert loaded.refresh_seconds == 30


def test_add_repo(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=["o/r"]))
    config = add_repo(path, "o/two")
    assert config.repos == ["o/r", "o/two"]
    assert load_config(path).repos == ["o/r", "o/two"]


def test_add_repo_duplicate_noop(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=["o/r"]))
    assert add_repo(path, "o/r").repos == ["o/r"]


def test_add_repo_invalid_raises(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config())
    with pytest.raises(ValidationError):
        add_repo(path, "garbage")


def test_remove_repo(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=["o/r", "o/two"]))
    config = remove_repo(path, "o/r")
    assert config.repos == ["o/two"]
    assert load_config(path).repos == ["o/two"]
