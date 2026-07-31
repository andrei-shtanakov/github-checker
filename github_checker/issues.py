"""Inbox-issue verbs: find a cross-repo request by slug, or create one.

`issue_lookup` narrows with GitHub search and then confirms by exact parse
of the structural block — search is substring-based and cannot be trusted
to mean what it appears to. `issue_create` builds the canonical body from
validated parts and re-checks for an existing match immediately before
creating, the same way `merge` re-checks its gate.
"""

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from github_checker.actions import ActionResult
from github_checker.ghcli import repo_slug, run_gh
from github_checker.inbox import canonical_body, slug_lines, valid_sender, valid_slug
from github_checker.models import IssueRef

SEARCH_FIELDS = "number,title,state,url,author,labels,body"


def _ref(data: dict[str, Any]) -> IssueRef:
    """Map one `gh search issues` item to an IssueRef."""
    return IssueRef(
        number=data["number"],
        title=data.get("title", ""),
        state=data.get("state", ""),
        url=data.get("url", ""),
        author=(data.get("author") or {}).get("login", ""),
        labels=[label["name"] for label in data.get("labels") or []],
    )


def _partition(candidates: Any, slug: str) -> tuple[list[IssueRef], list[IssueRef]]:
    """Split search candidates into confirmed matches and malformed ones.

    Raises `AttributeError`/`KeyError`/`TypeError`/`ValidationError` on any
    shape `gh` did not promise — a top-level object or scalar instead of a
    list, a `null` item, a candidate missing `number`, a `labels` entry
    missing `name`, or a `number` pydantic cannot coerce to `int`. The
    caller turns all of those into one failed `ActionResult`: a payload we
    cannot map is a search we could not read, not an empty one.
    """
    matches: list[IssueRef] = []
    malformed: list[IssueRef] = []
    for item in candidates:
        claimed = slug_lines(item.get("body") or "")
        if slug not in claimed:
            continue  # narrowed by substring search; not actually ours
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

    # No `--state` flag: `gh search issues` only accepts {open|closed} there
    # (`--state all` is rejected outright, exit 1) and omitting it already
    # returns both states — confirmed against a live repo with mixed-state
    # results. Passing "all" would make every real invocation fail closed.
    proc = run_gh(
        path,
        "search",
        "issues",
        "--repo",
        f"{owner}/{name}",
        "--label",
        "inbox",
        "--json",
        SEARCH_FIELDS,
        slug,
        binary=binary,
    )
    if proc.returncode != 0:
        return ActionResult(
            action="issue-lookup",
            dir=str(path),
            ok=False,
            error=proc.stderr.strip() or "gh search issues failed",
        )
    try:
        candidates = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return ActionResult(
            action="issue-lookup",
            dir=str(path),
            ok=False,
            error="unexpected non-JSON from gh search issues",
        )

    try:
        matches, malformed = _partition(candidates, slug)
    except (AttributeError, KeyError, TypeError, ValidationError) as err:
        return ActionResult(
            action="issue-lookup",
            dir=str(path),
            ok=False,
            error=f"unexpected issue payload shape: {err!r}",
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

    pre = issue_lookup(path, slug, binary=binary)
    if not pre.ok:
        return failed(pre.error or "slug lookup failed before create")
    if pre.matches:
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

    back = issue_lookup(path, slug, binary=binary)
    created_issue = back.matches[0] if back.ok and back.matches else None
    return ActionResult(
        action="issue-create",
        dir=str(path),
        ok=True,
        created=True,
        issue=created_issue,
        detail=(
            "created"
            if created_issue is not None
            else "created, but reading it back failed"
        ),
    )
