"""Inbox-issue verbs: find a cross-repo request by slug, or create one.

`issue_lookup` reads every inbox issue in one repo via `gh issue list` and
confirms each candidate by exact parse of the structural block — nothing
here trusts a label filter to mean identity. `issue_create` builds the
canonical body from validated parts and re-checks for an existing match
immediately before creating, the same way `merge` re-checks its gate.
"""

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from github_checker.actions import ActionResult
from github_checker.ghcli import repo_slug, run_gh
from github_checker.inbox import canonical_body, slug_lines, valid_sender, valid_slug
from github_checker.models import IssueRef

ISSUE_FIELDS = "number,title,state,url,author,labels,body"

# `gh issue list --limit` defaults to 30 and there is no "give me everything"
# mode — some cap always applies. This is deliberately generous for what an
# `inbox`-labelled backlog should ever hold, but the cap is still real, so
# returning exactly this many is treated as possibly-truncated rather than
# trusted as exhaustive (same idiom as prgate's checks_truncated /
# threads_truncated: exactly-at-the-cap is indistinguishable from "more").
ISSUE_LIST_LIMIT = 200


def _ref(data: dict[str, Any]) -> IssueRef:
    """Map one `gh issue list` item to an IssueRef."""
    return IssueRef(
        number=data["number"],
        title=data.get("title", ""),
        state=data.get("state", ""),
        url=data.get("url", ""),
        author=(data.get("author") or {}).get("login", ""),
        labels=[label["name"] for label in data.get("labels") or []],
    )


def _partition(candidates: Any, slug: str) -> tuple[list[IssueRef], list[IssueRef]]:
    """Split every inbox issue into confirmed matches and malformed ones.

    Raises `AttributeError`/`KeyError`/`TypeError`/`ValidationError` on any
    shape `gh` did not promise — a top-level object or scalar instead of a
    list, a `null` item, a candidate missing `number` or `body`, a `labels`
    entry missing `name`, or a `number` pydantic cannot coerce to `int`. The
    caller turns all of those into one failed `ActionResult`: a payload we
    cannot map is a search we could not read, not an empty one.

    `body` is indexed directly (`item["body"]`), not defaulted to `""`: it
    is the field identity is read from, the same as `number`. A candidate
    we cannot read a body for cannot be judged either way — defaulting it
    to empty would silently confirm "no claim", which is exactly the wrong
    direction to fail in here.
    """
    matches: list[IssueRef] = []
    malformed: list[IssueRef] = []
    for item in candidates:
        claimed = slug_lines(item["body"])
        if slug not in claimed:
            continue  # this issue's structural block doesn't claim our slug
        if len(claimed) > 1:
            malformed.append(_ref(item))
        else:
            matches.append(_ref(item))
    return matches, malformed


def issue_lookup(path: Path, slug: str, *, binary: str = "gh") -> ActionResult:
    """Find inbox issues claiming *slug* in this repo, in any state."""
    if not valid_slug(slug):
        return ActionResult(
            action="issue-lookup",
            dir=str(path),
            ok=False,
            error=f"invalid slug: {slug!r}",
        )
    resolved = repo_slug(path, binary=binary)
    if resolved is None:
        return ActionResult(
            action="issue-lookup",
            dir=str(path),
            ok=False,
            error="cannot resolve owner/repo for this clone",
        )
    owner, name = resolved

    # `gh issue list`, not `gh search issues`: the search index paginates
    # (30 by default, confirmed capped even with --limit) with no real
    # "give me everything" mode and can lag behind a just-made write, which
    # matters here because issue_create reads back immediately after
    # creating. `gh issue list` hits the API directly, takes a real
    # `--state all` (search's `--state` only accepts open|closed), and is
    # natively --repo-scoped. No free-text term: every inbox-labelled issue
    # in the repo comes back and slug_lines() alone decides membership.
    proc = run_gh(
        path,
        "issue",
        "list",
        "--repo",
        f"{owner}/{name}",
        "--state",
        "all",
        "--label",
        "inbox",
        "--limit",
        str(ISSUE_LIST_LIMIT),
        "--json",
        ISSUE_FIELDS,
        binary=binary,
    )
    if proc.returncode != 0:
        return ActionResult(
            action="issue-lookup",
            dir=str(path),
            ok=False,
            error=proc.stderr.strip() or "gh issue list failed",
        )
    try:
        candidates = json.loads(proc.stdout)
    except json.JSONDecodeError:
        # No `proc.stdout or "[]"` fallback: an rc=0 call with genuinely
        # empty stdout is not the same fact as a real `[]` from `gh` — the
        # fallback would launder "we got nothing back" into "we confirmed
        # there is nothing", which is exactly the fail-open this verb must
        # not do.
        return ActionResult(
            action="issue-lookup",
            dir=str(path),
            ok=False,
            error="unexpected non-JSON from gh issue list",
        )

    try:
        truncated = len(candidates) >= ISSUE_LIST_LIMIT
        matches, malformed = _partition(candidates, slug)
    except (AttributeError, KeyError, TypeError, ValidationError) as err:
        return ActionResult(
            action="issue-lookup",
            dir=str(path),
            ok=False,
            error=f"unexpected issue payload shape: {err!r}",
        )
    if truncated:
        # matches/malformed above were computed from a possibly-incomplete
        # list; discard them rather than return a partial result that would
        # read as clean. `matches` stays unset (None), a value distinct
        # from both a confirmed empty list and a confirmed non-empty one.
        return ActionResult(
            action="issue-lookup",
            dir=str(path),
            ok=False,
            error=(
                f"gh issue list returned {len(candidates)} issues, at or "
                f"above the {ISSUE_LIST_LIMIT}-issue cap; cannot confirm "
                "the inbox was read exhaustively"
            ),
        )

    return ActionResult(
        action="issue-lookup",
        dir=str(path),
        # a malformed candidate is neither a match nor an absence — it needs
        # a human, so it must not read as a clean "nothing found"
        ok=not malformed,
        error=(
            f"{len(malformed)} inbox issue(s) contain more than one 'slug:' line"
            if malformed
            else None
        ),
        matches=matches,
        malformed=malformed,
    )


def issue_create(
    path: Path,
    *,
    slug: str,
    sender: str,
    title: str,
    prose: str,
    binary: str = "gh",
) -> ActionResult:
    """Create an inbox issue for *slug*, unless one already exists.

    The structural block is written here from validated values — a caller
    supplies prose only, so a submitted form cannot forge `slug:`/`from:`.
    """

    def failed(error: str, created: bool | None = False) -> ActionResult:
        return ActionResult(
            action="issue-create",
            dir=str(path),
            ok=False,
            created=created,
            error=error,
        )

    if not valid_slug(slug):
        return failed(f"invalid slug: {slug!r}")
    if not valid_sender(sender):
        return failed(f"invalid from: {sender!r}")
    if not title.strip():
        return failed("title is required")

    resolved = repo_slug(path, binary=binary)
    if resolved is None:
        return failed("cannot resolve owner/repo for this clone")
    owner, name = resolved

    pre = issue_lookup(path, slug, binary=binary)
    if not pre.ok:
        return failed(pre.error or "slug lookup failed before create")
    if pre.matches:
        if len(pre.matches) > 1:
            # Several existing claimants is a conflict for the caller to
            # judge (same rule issue_lookup documents for itself) — silently
            # taking [0] would pick one arbitrarily and hide the other(s).
            return ActionResult(
                action="issue-create",
                dir=str(path),
                ok=False,
                created=False,
                matches=pre.matches,
                error=(
                    f"{len(pre.matches)} inbox issues already claim this "
                    "slug; ambiguous, not creating"
                ),
            )
        # someone got there first — the request exists, which is the point
        return ActionResult(
            action="issue-create",
            dir=str(path),
            ok=True,
            created=False,
            issue=pre.matches[0],
            detail="an inbox issue for this slug already exists",
        )

    proc = run_gh(
        path,
        "issue",
        "create",
        "--repo",
        f"{owner}/{name}",
        "--label",
        "inbox",
        "--title",
        title,
        "--body",
        canonical_body(slug, sender, prose),
        binary=binary,
    )
    if proc.returncode != 0:
        # the call broke: whether the issue landed is unknown, not "no"
        return failed(proc.stderr.strip() or "gh issue create failed", created=None)

    # The create call itself succeeded, so `created=True` is final — the
    # read-back below is diagnostic only. Its own failure modes must not be
    # collapsed into one message: "could not read", "read cleanly but found
    # nothing (yet)", and "found a malformed claimant" are different facts
    # for a caller deciding whether to look again.
    back = issue_lookup(path, slug, binary=binary)
    if not back.ok:
        issue = None
        detail = (
            "created, but the read-back found a malformed claimant"
            if back.malformed
            else "created, but reading it back failed"
        )
    elif not back.matches:
        issue, detail = None, "created, but reading it back found nothing yet"
    elif len(back.matches) == 1:
        issue, detail = back.matches[0], "created"
    else:
        issue = None
        detail = "created, but reading it back found more than one match"

    return ActionResult(
        action="issue-create",
        dir=str(path),
        ok=True,
        created=True,
        issue=issue,
        detail=detail,
    )
