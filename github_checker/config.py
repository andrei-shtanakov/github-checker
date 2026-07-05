"""Load and persist repos.toml."""

import tomllib
from pathlib import Path

import tomli_w

from github_checker.models import Config


def load_config(path: Path) -> Config:
    """Read config from *path*, creating an empty one if missing."""
    if not path.exists():
        config = Config()
        save_config(path, config)
        return config
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return Config(**data)


def save_config(path: Path, config: Config) -> None:
    """Write *config* to *path* as TOML."""
    path.write_text(tomli_w.dumps(config.model_dump()), encoding="utf-8")


def add_repo(path: Path, name: str) -> Config:
    """Add *name* to the config file; duplicates are ignored."""
    config = load_config(path)
    if name in config.repos:
        return config
    updated = Config(
        repos=[*config.repos, name],
        refresh_seconds=config.refresh_seconds,
    )
    save_config(path, updated)
    return updated


def remove_repo(path: Path, name: str) -> Config:
    """Remove *name* from the config file if present."""
    config = load_config(path)
    updated = config.model_copy(
        update={"repos": [r for r in config.repos if r != name]}
    )
    save_config(path, updated)
    return updated
