"""Console entrypoint."""

import argparse
import asyncio
import sys
import tomllib
from pathlib import Path

from pydantic import ValidationError

from github_checker.config import (
    default_config_path,
    load_config,
    resolve_config_path,
)
from github_checker.github import gh_ready


def _run_tui(config: Path | None) -> None:
    """Verify gh CLI and config, run the dashboard."""
    from github_checker.app import GithubCheckerApp

    error = gh_ready()
    if error is not None:
        print(error, file=sys.stderr)
        raise SystemExit(1)
    config_path = resolve_config_path(config)
    try:
        load_config(config_path)
    except (tomllib.TOMLDecodeError, ValidationError) as err:
        print(f"Некорректный repos.toml ({config_path}): {err}", file=sys.stderr)
        raise SystemExit(1) from err
    GithubCheckerApp(config_path).run()


def _run_snapshot(workspace: Path, local_only: bool, indent: int | None) -> None:
    """Print a WorkspaceSnapshot as JSON; degrades gracefully without gh."""
    from github_checker.snapshot import build_snapshot

    if not workspace.is_dir():
        print(f"Не каталог: {workspace}", file=sys.stderr)
        raise SystemExit(1)
    snapshot = asyncio.run(build_snapshot(workspace, include_github=not local_only))
    print(snapshot.model_dump_json(indent=indent))


def main() -> None:
    """Parse args and dispatch: TUI (default) or headless snapshot."""
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
    sub = parser.add_subparsers(dest="command")
    snap = sub.add_parser(
        "snapshot",
        help="print the state of every repo in a polyrepo workspace as JSON",
    )
    snap.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="polyrepo root to scan for */.git (default: cwd)",
    )
    snap.add_argument(
        "--local-only",
        action="store_true",
        help="skip GitHub API entirely (git-only snapshot)",
    )
    snap.add_argument(
        "--indent",
        type=int,
        default=2,
        help="JSON indent, 0 for compact (default: 2)",
    )
    args = parser.parse_args()
    if args.command == "snapshot":
        _run_snapshot(args.workspace, args.local_only, args.indent or None)
    else:
        _run_tui(args.config)


if __name__ == "__main__":
    main()
