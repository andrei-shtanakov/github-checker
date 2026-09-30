"""merged-prs: PRs merged since a moment, across the clone's owner, with who merged.

Consumer: dispatcher's factory floor (slice C2) — "what did the agent merge
in the last 24 h". Only GraphQL search can answer it: the REST search can
filter by merge time but returns no merger, and `mergedBy` is a GraphQL-only
field. The verb reports every merge with its `merged_by`; filtering by login
is the consumer's policy, not this verb's.

One paginated `gh api graphql` call covers the whole owner. The answer
follows the issue-lookup idiom: `[]` is a confirmed empty search, and
anything short of an exhaustive read (a failed call, unparseable output, a
malformed node, fewer nodes than the search counted, a count above the
search's 1000-result ceiling) is `merges = None`, never a shorter list.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from github_checker.actions import ActionResult, result_for
from github_checker.ghcli import repo_slug, run_gh
from github_checker.models import MergedPr

# GitHub search returns at most this many results however it is paginated.
SEARCH_CEILING = 1000

QUERY = (
    "query($q: String!, $endCursor: String) {"
    " search(query: $q, type: ISSUE, first: 100, after: $endCursor) {"
    " issueCount pageInfo { hasNextPage endCursor }"
    " nodes { ... on PullRequest { number title url mergedAt"
    " mergedBy { login } repository { nameWithOwner } } } } }"
)


def parse_since(value: str) -> datetime | None:
    """A timezone-aware ISO-8601 instant, or None when *value* is not one.

    A naive time is refused: the search qualifier would read it as UTC while
    the caller may have meant local time — a silent shift of the window.
    """
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def merged_prs(path: Path, since: str, *, binary: str = "gh") -> ActionResult:
    """Every PR merged at or after *since* under the owner of the clone at *path*."""
    moment = parse_since(since)
    if moment is None:
        return result_for(
            "merged-prs",
            path,
            ok=False,
            error=f"--since must be a timezone-aware ISO-8601 time: {since!r}",
        )
    resolved = repo_slug(path, binary=binary)
    if resolved is None:
        return result_for(
            "merged-prs", path, ok=False, error="cannot resolve owner for this clone"
        )
    owner, _ = resolved
    stamp = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    proc = run_gh(
        path,
        "api",
        "graphql",
        "--paginate",
        "--slurp",
        "-f",
        f"q=is:pr is:merged user:{owner} merged:>={stamp}",
        "-f",
        f"query={QUERY}",
        binary=binary,
    )
    if proc.returncode != 0:
        return result_for(
            "merged-prs",
            path,
            ok=False,
            error=proc.stderr.strip() or "gh api graphql failed",
        )
    try:
        pages = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return result_for(
            "merged-prs",
            path,
            ok=False,
            error="unexpected non-JSON from gh api graphql",
        )
    try:
        merges = _merges(pages)
    except (
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        ValidationError,
    ) as err:
        return result_for(
            "merged-prs",
            path,
            ok=False,
            error=f"unexpected search payload shape: {err!r}",
        )
    if isinstance(merges, str):
        return result_for("merged-prs", path, ok=False, error=merges)
    return result_for("merged-prs", path, ok=True, merges=merges)


def _merges(pages: list[dict[str, Any]]) -> list[MergedPr] | str:
    """The merges of every page, or why the read was not exhaustive.

    Raises on a shape gh did not promise.
    """
    searches = [page["data"]["search"] for page in pages]
    counted = int(searches[0]["issueCount"])
    if counted > SEARCH_CEILING:
        return (
            f"search counted {counted} merges, above the {SEARCH_CEILING}-result "
            "ceiling; cannot read them all"
        )
    nodes = [node for search in searches for node in search["nodes"]]
    if len(nodes) != counted:
        return (
            f"search counted {counted} merges but returned {len(nodes)}; "
            "cannot confirm the read was exhaustive"
        )
    return [_merge(node) for node in nodes]


def _merge(node: dict[str, Any]) -> MergedPr:
    """Map one search node; raises on a shape gh did not promise."""
    merger = node["mergedBy"]
    return MergedPr(
        repo=node["repository"]["nameWithOwner"],
        number=int(node["number"]),
        title=node.get("title") or "",
        url=node["url"],
        merged_at=node["mergedAt"],
        # null: the merging account no longer exists (GitHub's "ghost").
        merged_by=None if merger is None else merger["login"],
    )
