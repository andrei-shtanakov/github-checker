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

from github_checker.actions import ActionResult
from github_checker.ghcli import repo_slug, run_gh
from github_checker.inbox import slug_lines, valid_slug
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

    return ActionResult(
        action="issue-lookup",
        dir=str(path),
        # a malformed candidate is neither a match nor an absence — it needs
        # a human, so it must not read as a clean "nothing found"
        ok=not malformed,
        error=(
            f"{len(malformed)} inbox issue(s) carry more than one slug: line"
            if malformed
            else None
        ),
        matches=matches,
        malformed=malformed,
    )
