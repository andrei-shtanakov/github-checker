from pathlib import Path

import pytest
from pydantic import ValidationError

from github_checker.config import add_repo, load_config, remove_repo, save_config
from github_checker.models import Config


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
