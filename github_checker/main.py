"""Console entrypoint."""

import argparse
import asyncio
import sys
import tomllib
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

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


# A refusal raised before the verb ever ran: argv could not be parsed, so no
# verb-specific field has a value yet — not even an unknown one. Its shape is
# the envelope plus `error`; `action` still names the verb that was attempted,
# which is what a consumer probing for one needs to see.
#
# The caller declares this variant explicitly rather than letting _emit infer
# it from the shape: a verb whose own failure happens to set only `error`
# (`pull` on a non-repository does exactly that) would otherwise slip past
# the shape check disguised as a parse error.
PRE_DISPATCH_REFUSAL = {"action", "dir", "ok", "error"}


def _emit(result: "ActionResult", *, pre_dispatch: bool = False) -> None:
    """Print one JSON ActionResult and honour the exit-code contract.

    contracts/actions/v1 wire semantics:
        field absent         -> the verb has no such concept
        field present, null  -> applicable, value unknown
        field present, false -> applicable, definitely negative

    `exclude_unset=True` makes the model the single source of truth: what a
    call site explicitly set is what ships. The assertion below is the guard
    that keeps that honest — a verb whose result is missing an applicable
    field, or carrying a foreign one, fails here instead of shipping a
    payload whose shape quietly contradicts the contract. Note that
    `model_copy(update=...)` *adds* to `model_fields_set`, so a field set
    once keeps serialising even after its value changes.
    """
    import json

    from github_checker.actions import contracted_keys

    payload = result.model_dump(mode="json", exclude_unset=True)
    expected = contracted_keys(result.action)
    if pre_dispatch:
        expected = PRE_DISPATCH_REFUSAL
    if set(payload) != expected:
        # Drift inside this binary must not become a traceback with no JSON:
        # that is the very failure the contract exists to prevent, and a
        # consumer would see an empty stdout instead of a readable refusal.
        # Report it *through* the contract, in the shape that is valid for
        # any action, and fail closed.
        missing = sorted(expected - set(payload))
        foreign = sorted(set(payload) - expected)
        payload = {
            "action": result.action,
            "dir": result.dir,
            "ok": False,
            "error": (
                f"contract violation: {result.action} did not match its "
                f"contracted shape; missing={missing} foreign={foreign}"
            ),
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    if not result.ok:
        raise SystemExit(1)


def _run_action(action: str, directory: Path) -> None:
    """Run a headless whitelist action and print its JSON result."""
    from github_checker.actions import open_pr, pull

    result = pull(directory) if action == "pull" else open_pr(directory)
    _emit(result)


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
    _emit(result)


def _run_pr_detail(args: argparse.Namespace) -> None:
    """Read one PR and print it inside an ActionResult envelope."""
    from github_checker import prgate
    from github_checker.actions import result_for

    # --file-limit/--diff-lines default to None on the parser so prgate's own
    # FILE_LIMIT/DIFF_LINE_LIMIT stay the single source of truth for the
    # numbers; only override them when the caller actually gave a value.
    file_limit = args.file_limit if args.file_limit is not None else prgate.FILE_LIMIT
    diff_line_limit = (
        args.diff_lines if args.diff_lines is not None else prgate.DIFF_LINE_LIMIT
    )

    # Not argparse `type=`/`choices=` validation: argparse would exit(2) with
    # a usage message on stderr, breaking the "exactly one JSON ActionResult
    # on stdout" contract (see the --if-match comment on the propose-pr
    # parser). A value below 1 isn't just out of range, it silently does the
    # wrong thing: `list[:-5]` drops the last 5 items instead of limiting to
    # a count, and inside truncate_diff `len(lines) > -5` is always true, so
    # diff_truncated would be reported regardless of what was actually cut.
    # 0 is rejected too — a zero-file listing or zero-line diff conveys
    # nothing, so it's as meaningless as a negative value here.
    for flag, value in (
        ("--file-limit", file_limit),
        ("--diff-lines", diff_line_limit),
    ):
        if value < 1:
            _emit(
                result_for(
                    "pr-detail",
                    args.dir,
                    ok=False,
                    error=f"{flag} must be >= 1, got {value}",
                )
            )
            return

    try:
        detail = prgate.pr_detail(
            args.dir,
            args.pr,
            file_limit=file_limit,
            diff_line_limit=diff_line_limit,
        )
    except prgate.GateUnavailable as err:
        _emit(result_for("pr-detail", args.dir, ok=False, error=str(err)))
        return
    _emit(result_for("pr-detail", args.dir, ok=True, pr_detail=detail))


def _run_merge(args: argparse.Namespace) -> None:
    """Squash-merge one PR behind the fail-closed gate."""
    from github_checker import prgate
    from github_checker.actions import result_for

    if not args.if_head:
        _emit(
            result_for(
                "merge",
                args.dir,
                ok=False,
                merged=False,
                # Every other merge refusal goes through prgate._merge_failure,
                # which sets this; matching it here means a JSON consumer never
                # has to branch on a null it could otherwise treat as known.
                local_sync="not_attempted",
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


def _run_issue_lookup(args: argparse.Namespace) -> None:
    """Find inbox issues claiming a slug; print the JSON result."""
    from github_checker.actions import result_for
    from github_checker.issues import issue_lookup

    if not args.slug:
        _emit(
            result_for("issue-lookup", args.dir, ok=False, error="--slug is required")
        )
        return
    _emit(issue_lookup(args.dir, args.slug))


def _run_issue_create(args: argparse.Namespace) -> None:
    """Create an inbox issue from validated parts plus a prose file."""
    from github_checker.actions import result_for
    from github_checker.issues import issue_create

    def refuse(error: str) -> None:
        _emit(
            result_for(
                "issue-create",
                args.dir,
                ok=False,
                created=False,
                error=error,
            )
        )

    for flag, value in (
        ("--slug", args.slug),
        ("--from", args.sender),
        ("--title", args.title),
        ("--body-file", args.body_file),
    ):
        if not value:
            refuse(f"{flag} is required")
            return
    try:
        # encoding pinned, not left to the locale: the non-UTF-8 refusal
        # below must behave the same on every machine, and a headless
        # contract that decodes differently per host is not a contract
        prose = Path(args.body_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        # UnicodeDecodeError is a ValueError, not an OSError, so it must be
        # caught here explicitly: this read happens before any mutation, so
        # the truthful `created` is False (nothing attempted), not the
        # `_dispatch_guarded` catch-all's None ("transport broke mid-call,
        # may have landed") — that distinction is the whole point of the
        # three-valued `created` contract, and only the site of the read
        # knows which one it is.
        refuse(f"cannot read --body-file: {err}")
        return
    _emit(
        issue_create(
            args.dir,
            slug=args.slug,
            sender=args.sender,
            title=args.title,
            prose=prose,
        )
    )


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
    from github_checker.actions import result_for

    try:
        handler()
    except SystemExit:
        # SystemExit derives from BaseException, not Exception, so the
        # `except Exception` below would never catch it regardless of
        # ordering; this clause is not load-bearing, just self-documentation
        # that _emit's own ok=False exit is meant to propagate untouched.
        raise
    except Exception as err:
        # The frame list belongs on stderr, outside the stdout JSON contract:
        # it keeps the bug locatable without adding a second shape to stdout.
        traceback.print_exc()
        # Built through result_for: an unhandled exception means this verb's
        # own fields are *unknown*, not inapplicable. Emitting a bare
        # envelope would tell the consumer the verb has no concept of, say,
        # `merged` — when the truth is that we cannot say what happened.
        _emit(
            result_for(
                action,
                directory,
                ok=False,
                error=f"{type(err).__name__}: {err}",
            )
        )


# Every verb that prints an ActionResult. `snapshot` is deliberately absent:
# it has its own shape, and the TUI has none at all.
ACTION_VERBS = (
    "pull",
    "open-pr",
    "propose-pr",
    "pr-detail",
    "merge",
    "post-merge-sync",
    "issue-lookup",
    "issue-create",
)


class _ArgvRefused(Exception):
    """argparse rejected argv before any handler could run."""


class _JsonParser(argparse.ArgumentParser):
    """An ArgumentParser that refuses through the JSON contract.

    argparse's own `error()` writes usage to stderr and calls `exit(2)`,
    printing no JSON at all — so a value beginning with `-` (`--slug --from`),
    an unknown verb, a missing or extra positional each bypassed every
    handler's validation and broke the "exactly one JSON on stdout" contract
    that machine consumers depend on. `error()` is the single funnel for all
    of them; `--help` goes through `exit()` instead and is untouched.
    """

    def error(self, message: str) -> NoReturn:
        raise _ArgvRefused(message)


def _refuse_argv(argv: list[str], message: str) -> None:
    """Emit the JSON refusal argparse would otherwise have skipped.

    Built directly rather than through `result_for`: this is the
    pre-dispatch variant, whose point is that no verb field has a value
    yet — not even an unknown one. Filling the attempted verb's set with
    nulls would claim more than we know.
    """
    from github_checker.actions import ActionResult

    positional = [a for a in argv if not a.startswith("-")]
    verb = positional[0] if positional else ""
    _emit(
        ActionResult(
            # `action` is best-effort: an unknown verb is reported as given,
            # so a consumer probing for a verb this version lacks sees which
            # one was refused rather than a blank.
            action=verb or "unknown",
            dir=positional[1] if len(positional) > 1 else "",
            ok=False,
            error=f"invalid command line: {message}",
        ),
        pre_dispatch=True,
    )


def main() -> None:
    """Parse args and dispatch: TUI (default) or headless snapshot/actions."""
    parser = _JsonParser(
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
    # default=None (not a literal number): prgate.pr_detail()'s own
    # FILE_LIMIT/DIFF_LINE_LIMIT stay the single source of truth; _run_pr_detail
    # passes an override through only when the caller actually gave one.
    detail_p.add_argument(
        "--file-limit",
        type=int,
        default=None,
        help="max files listed (default: pr_detail's own limit)",
    )
    detail_p.add_argument(
        "--diff-lines",
        type=int,
        default=None,
        help="max diff lines (default: pr_detail's own limit)",
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

    lookup_p = sub.add_parser(
        "issue-lookup",
        help="find inbox issues claiming a slug in this repo; prints JSON",
    )
    lookup_p.add_argument("dir", type=Path, help="path to the local clone")
    # NOT required=True anywhere below: argparse would exit(2) with usage on
    # stderr and break the headless JSON contract; the handlers validate.
    lookup_p.add_argument("--slug", default=None, help="canonical item slug")

    create_p = sub.add_parser(
        "issue-create",
        help="create an inbox issue for a slug, unless one exists; prints JSON",
    )
    create_p.add_argument("dir", type=Path, help="path to the local clone")
    create_p.add_argument("--slug", default=None, help="canonical item slug")
    create_p.add_argument(
        "--from",
        dest="sender",
        default=None,
        help="requesting repo, optionally repo#slug",
    )
    create_p.add_argument("--title", default=None, help="issue title")
    create_p.add_argument(
        "--body-file",
        dest="body_file",
        default=None,
        help="file holding the prose; the structural block is built for you",
    )

    try:
        args = parser.parse_args()
    except _ArgvRefused as err:
        _refuse_argv(sys.argv[1:], str(err))
        return
    if args.command == "snapshot":
        _run_snapshot(args.workspace, args.local_only, args.indent or None)
    elif args.command in ("pull", "open-pr"):
        _dispatch_guarded(
            args.command, args.dir, lambda: _run_action(args.command, args.dir)
        )
    elif args.command == "propose-pr":
        _dispatch_guarded("propose-pr", args.dir, lambda: _run_propose(args))
    elif args.command == "pr-detail":
        _dispatch_guarded("pr-detail", args.dir, lambda: _run_pr_detail(args))
    elif args.command == "merge":
        _dispatch_guarded("merge", args.dir, lambda: _run_merge(args))
    elif args.command == "post-merge-sync":
        _dispatch_guarded(
            "post-merge-sync", args.dir, lambda: _run_post_merge_sync(args)
        )
    elif args.command == "issue-lookup":
        _dispatch_guarded("issue-lookup", args.dir, lambda: _run_issue_lookup(args))
    elif args.command == "issue-create":
        _dispatch_guarded("issue-create", args.dir, lambda: _run_issue_create(args))
    else:
        _run_tui(args.config)


if __name__ == "__main__":
    main()
