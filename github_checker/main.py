"""Console entrypoint."""

import argparse
import sys
from pathlib import Path

from github_checker.app import GithubCheckerApp
from github_checker.github import gh_ready


def main() -> None:
    """Parse args, verify gh CLI, run the dashboard."""
    parser = argparse.ArgumentParser(
        prog="github-checker",
        description="TUI monitor for multiple GitHub repositories.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("repos.toml"),
        help="path to repos.toml (default: ./repos.toml)",
    )
    args = parser.parse_args()
    error = gh_ready()
    if error is not None:
        print(error, file=sys.stderr)
        raise SystemExit(1)
    GithubCheckerApp(args.config).run()


if __name__ == "__main__":
    main()
