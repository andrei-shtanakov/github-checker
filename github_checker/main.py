"""Console entrypoint."""

import argparse
import sys
import tomllib
from pathlib import Path

from pydantic import ValidationError

from github_checker.app import GithubCheckerApp
from github_checker.config import (
    default_config_path,
    load_config,
    resolve_config_path,
)
from github_checker.github import gh_ready


def main() -> None:
    """Parse args, verify gh CLI and config, run the dashboard."""
    parser = argparse.ArgumentParser(
        prog="github-checker",
        description="TUI monitor for multiple GitHub repositories.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"path to repos.toml (default: {default_config_path()})",
    )
    args = parser.parse_args()
    error = gh_ready()
    if error is not None:
        print(error, file=sys.stderr)
        raise SystemExit(1)
    config_path = resolve_config_path(args.config)
    try:
        load_config(config_path)
    except (tomllib.TOMLDecodeError, ValidationError) as err:
        print(f"Некорректный repos.toml ({config_path}): {err}", file=sys.stderr)
        raise SystemExit(1) from err
    GithubCheckerApp(config_path).run()


if __name__ == "__main__":
    main()
