"""Console entrypoint."""

import argparse
import asyncio
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from github_checker.config import (
    default_config_path,
    load_config,
    resolve_config_path,
)
from github_checker.github import gh_ready

if TYPE_CHECKING:
    from github_checker.actions import ActionResult


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


def _emit(result: "ActionResult") -> None:
    """Print one JSON ActionResult and honour the exit-code contract."""
    print(result.model_dump_json(indent=2))
    if not result.ok:
        raise SystemExit(1)


def _run_pr_detail(args: argparse.Namespace) -> None:
    """Read one PR and print it inside an ActionResult envelope."""
    from github_checker import prgate
    from github_checker.actions import ActionResult

    try:
        detail = prgate.pr_detail(
            args.dir,
            args.pr,
            file_limit=args.file_limit,
            diff_line_limit=args.diff_lines,
        )
    except prgate.GateUnavailable as err:
        _emit(
            ActionResult(
                action="pr-detail", dir=str(args.dir), ok=False, error=str(err)
            )
        )
        return
    _emit(
        ActionResult(action="pr-detail", dir=str(args.dir), ok=True, pr_detail=detail)
    )


def _run_merge(args: argparse.Namespace) -> None:
    """Squash-merge one PR behind the fail-closed gate."""
    from github_checker import prgate
    from github_checker.actions import ActionResult

    if not args.if_head:
        _emit(
            ActionResult(
                action="merge",
                dir=str(args.dir),
                ok=False,
                merged=False,
                error="--if-head is required",
            )
        )
        return
    result = prgate.merge_pr(args.dir, args.pr, if_head=args.if_head)
    if result.pr_detail is not None and result.pr_detail.diff is not None:
        # A refusal embeds the full PrDetail (checks, threads, gate_failed
        # reasons) for diagnosis, but its diff can run to 2000 lines / 200KB.
        # `merge` answers "why refused", not "what changed" — a caller who
        # wants the diff itself already has `pr-detail` for that, and a
        # human staring at a terminal shouldn't get a diff dump instead of
        # a reason. `diff=None` is the model's existing "not fetched" state,
        # so this doesn't add a new, ambiguous shape to the contract.
        result = result.model_copy(
            update={"pr_detail": result.pr_detail.model_copy(update={"diff": None})}
        )
    _emit(result)


def _run_post_merge_sync(args: argparse.Namespace) -> None:
    """Return the clone to a freshly pulled default branch."""
    from github_checker.actions import post_merge_sync

    _emit(post_merge_sync(args.dir))


def _dispatch_guarded(
    action: str, directory: Path, handler: Callable[[], None]
) -> None:
    """Run a verb handler; turn any exception `_emit` didn't already catch
    into a failed ActionResult instead of a bare traceback.

    Known failure modes (GateUnavailable, missing --if-head, ...) are already
    handled inside each `_run_*` and reach `_emit` as JSON. This is the
    outermost net for everything else: it protects the "one JSON ActionResult
    on stdout" contract from an unforeseen bug, at the cost of turning that
    bug into a quieter failure. The exception type stays in `error` so it's
    still diagnosable, not just swallowed.
    """
    from github_checker.actions import ActionResult

    try:
        handler()
    except SystemExit:
        raise  # _emit's own ok=False exit — not an unforeseen failure
    except Exception as err:
        _emit(
            ActionResult(
                action=action,
                dir=str(directory),
                ok=False,
                error=f"{type(err).__name__}: {err}",
            )
        )


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
    detail_p = sub.add_parser(
        "pr-detail",
        help="read one PR (state, checks, files, diff, review threads) as JSON",
    )
    detail_p.add_argument("dir", type=Path, help="path to the local clone")
    detail_p.add_argument("pr", type=int, help="pull request number")
    detail_p.add_argument(
        "--file-limit", type=int, default=100, help="max files listed (default: 100)"
    )
    detail_p.add_argument(
        "--diff-lines", type=int, default=2000, help="max diff lines (default: 2000)"
    )

    merge_p = sub.add_parser(
        "merge", help="squash-merge a PR if every gate predicate holds; JSON result"
    )
    merge_p.add_argument("dir", type=Path, help="path to the local clone")
    merge_p.add_argument("pr", type=int, help="pull request number")
    # NOT required=True: argparse would exit(2) with usage on stderr and break
    # the headless JSON contract; _run_merge validates and returns ok=False.
    merge_p.add_argument(
        "--if-head", dest="if_head", default=None, help="head SHA the caller saw"
    )

    sync_p = sub.add_parser(
        "post-merge-sync",
        help="switch to the default branch, ff-pull, prune merged branches",
    )
    sync_p.add_argument("dir", type=Path, help="path to the local clone")

    args = parser.parse_args()
    if args.command == "snapshot":
        _run_snapshot(args.workspace, args.local_only, args.indent or None)
    elif args.command in ("pull", "open-pr"):
        _run_action(args.command, args.dir)
    elif args.command == "propose-pr":
        _run_propose(args)
    elif args.command == "pr-detail":
        _dispatch_guarded("pr-detail", args.dir, lambda: _run_pr_detail(args))
    elif args.command == "merge":
        _dispatch_guarded("merge", args.dir, lambda: _run_merge(args))
    elif args.command == "post-merge-sync":
        _dispatch_guarded(
            "post-merge-sync", args.dir, lambda: _run_post_merge_sync(args)
        )
    else:
        _run_tui(args.config)


if __name__ == "__main__":
    main()
