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


def _run_action(action: str, directory: Path) -> None:
    """Run a headless whitelist action and print its JSON result."""
    from github_checker.actions import open_pr, pull

    result = pull(directory) if action == "pull" else open_pr(directory)
    print(result.model_dump_json(indent=2))
    if not result.ok:
        raise SystemExit(1)


def _run_propose(args: argparse.Namespace) -> None:
    """Run propose-pr and print its JSON result (exit 1 on failure)."""
    from github_checker.propose import propose_pr

    result = propose_pr(
        args.dir,
        message=args.message,
        edit_args=args.edit,
        if_match_args=args.if_match,
        branch=args.branch,
    )
    print(result.model_dump_json(indent=2))
    if not result.ok:
        raise SystemExit(1)


def main() -> None:
    """Parse args and dispatch: TUI (default) or headless snapshot/actions."""
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
    for name, help_text in (
        ("pull", "fast-forward pull of one repo (headless twin of TUI key S)"),
        ("open-pr", "create (or report) a PR for the repo's current branch"),
    ):
        act = sub.add_parser(name, help=help_text + "; prints a JSON result")
        act.add_argument("dir", type=Path, help="path to the local clone")
    prop = sub.add_parser(
        "propose-pr",
        help=(
            "apply explicit file content in a temp worktree off the default "
            "branch, push a fresh branch, open a PR; prints a JSON result"
        ),
    )
    prop.add_argument("dir", type=Path, help="path to the local clone")
    prop.add_argument("--message", required=True, help="commit message (PR title)")
    prop.add_argument(
        "--edit",
        action="append",
        default=[],
        metavar="REPO_PATH=CONTENT_FILE",
        help="file to create/replace (repeatable)",
    )
    # NOT required=True: argparse would exit(2) with a usage message on
    # stderr, breaking the headless JSON contract. propose_pr() itself
    # returns ActionResult(ok=False, error="at least one --edit is
    # required") -> JSON on stdout + exit 1, like every other failure.
    prop.add_argument(
        "--if-match",
        action="append",
        default=[],
        dest="if_match",
        metavar="REPO_PATH=SHA256",
        help="stale-base guard: sha256 of the base content the caller saw",
    )
    prop.add_argument(
        "--branch",
        default=None,
        help="head branch name (generated if omitted)",
    )
    args = parser.parse_args()
    if args.command == "snapshot":
        _run_snapshot(args.workspace, args.local_only, args.indent or None)
    elif args.command in ("pull", "open-pr"):
        _run_action(args.command, args.dir)
    elif args.command == "propose-pr":
        _run_propose(args)
    else:
        _run_tui(args.config)


if __name__ == "__main__":
    main()
