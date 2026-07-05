"""Load and persist repos.toml."""

import os
import tomllib
from pathlib import Path

import tomli_w

from github_checker.models import Config


def default_config_path() -> Path:
    """User-level config location: $XDG_CONFIG_HOME/github-checker/repos.toml."""
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "github-checker" / "repos.toml"


def resolve_config_path(explicit: Path | None) -> Path:
    """Return the explicit path, or the user-level default.

    On first use, a legacy ./repos.toml is migrated to the default location
    so configs created by older versions are not lost.
    """
    if explicit is not None:
        return explicit
    target = default_config_path()
    legacy = Path("repos.toml")
    if not target.exists() and legacy.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            legacy.unlink()
        except OSError:
            pass  # migration succeeded; leftover legacy file is only cosmetic
    return target


def load_config(path: Path) -> Config:
    """Read config from *path*, creating an empty one if missing."""
    if not path.exists():
        config = Config()
        save_config(path, config)
        return config
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return Config(**data)


def save_config(path: Path, config: Config) -> None:
    """Write *config* to *path* as TOML, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
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
