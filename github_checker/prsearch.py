"""pr-search: open PRs carrying one label across the clone's owner.

One `gh search prs` call covers every repository of the owner — the consumer
(dispatcher's human queue) polls, and a per-repo walk would multiply the
calls by the fleet size. Each hit is then enriched with what a human needs to
merge it safely (the head SHA, which the consumer pins as devtools
`human-merge.sh --expect-head`) and to age the wait (when the label was last
added). That enrichment costs two `gh api` calls per hit — bounded by the
labelled set, which for a human-merge label is a handful, not the fleet; a
consumer that polls should cache the answer. An enrichment read that fails
leaves its fields None — unknown — while the PR itself stays in the list: it
was found.

The search itself follows the issue-lookup idiom: `[]` is a confirmed empty
answer, and anything short of an exhaustive read (a failed call, unparseable
output, a malformed hit, the result cap reached) is `prs = None`, never a
shorter list.
"""

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from github_checker.actions import ActionResult, result_for
from github_checker.ghcli import repo_slug, run_gh
from github_checker.models import PrRef

# `gh search prs --limit` has no "everything" mode; returning exactly this
# many is treated as possibly-truncated (same idiom as ISSUE_LIST_LIMIT).
PR_SEARCH_LIMIT = 100
SEARCH_FIELDS = "number,title,url,repository"
LABEL_MAX_LEN = 100


def _valid_label(label: str) -> bool:
    # A comma is refused: `gh search prs --label` treats it as a list
    # separator, so "a,b" would silently search two labels.
    return (
        0 < len(label) <= LABEL_MAX_LEN
        and label.isprintable()
        and label == label.strip()
        and "," not in label
    )


def pr_search(path: Path, label: str, *, binary: str = "gh") -> ActionResult:
    """Every open PR labelled *label* under the owner of the clone at *path*."""
    if not _valid_label(label):
        return result_for(
            "pr-search", path, ok=False, error=f"invalid label: {label!r}"
        )
    resolved = repo_slug(path, binary=binary)
    if resolved is None:
        return result_for(
            "pr-search", path, ok=False, error="cannot resolve owner for this clone"
        )
    owner, _ = resolved
    proc = run_gh(
        path,
        "search",
        "prs",
        "--owner",
        owner,
        "--label",
        label,
        "--state",
        "open",
        "--limit",
        str(PR_SEARCH_LIMIT),
        "--json",
        SEARCH_FIELDS,
        binary=binary,
    )
    if proc.returncode != 0:
        return result_for(
            "pr-search",
            path,
            ok=False,
            error=proc.stderr.strip() or "gh search prs failed",
        )
    try:
        hits = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return result_for(
            "pr-search", path, ok=False, error="unexpected non-JSON from gh search prs"
        )
    try:
        if len(hits) >= PR_SEARCH_LIMIT:
            return result_for(
                "pr-search",
                path,
                ok=False,
                error=(
                    f"gh search prs returned {len(hits)} PRs, at or above the "
                    f"{PR_SEARCH_LIMIT}-PR cap; cannot confirm the search was "
                    "exhaustive"
                ),
            )
        prs = [_enriched(path, hit, label, binary) for hit in hits]
    except (AttributeError, KeyError, TypeError, ValueError, ValidationError) as err:
        return result_for(
            "pr-search",
            path,
            ok=False,
            error=f"unexpected search payload shape: {err!r}",
        )
    return result_for("pr-search", path, ok=True, prs=prs)


def _enriched(path: Path, hit: dict[str, Any], label: str, binary: str) -> PrRef:
    """Map one search hit; raises on a shape gh did not promise."""
    repo = hit["repository"]["nameWithOwner"]
    number = int(hit["number"])
    head_sha, head_ref = _head(path, repo, number, binary)
    return PrRef(
        repo=repo,
        number=number,
        title=hit.get("title") or "",
        url=hit["url"],
        head_sha=head_sha,
        head_ref=head_ref,
        labeled_at=_labeled_at(path, repo, number, label, binary),
    )


def _head(
    path: Path, repo: str, number: int, binary: str
) -> tuple[str | None, str | None]:
    """(head sha, head ref) of one PR, or (None, None) when unreadable."""
    proc = run_gh(path, "api", f"repos/{repo}/pulls/{number}", binary=binary)
    if proc.returncode != 0:
        return None, None
    try:
        head = json.loads(proc.stdout)["head"]
        sha, ref = head.get("sha"), head.get("ref")
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
        return None, None
    return (
        sha if isinstance(sha, str) else None,
        ref if isinstance(ref, str) else None,
    )


def _labeled_at(
    path: Path, repo: str, number: int, label: str, binary: str
) -> str | None:
    """When *label* was LAST added to the PR, or None when unknown.

    The label is embedded in the jq filter as a JSON string literal
    (`json.dumps`), never spliced raw — a label is data, not filter syntax.
    """
    jq = (
        f'.[] | select(.event == "labeled" and .label.name == {json.dumps(label)})'
        " | .created_at"
    )
    proc = run_gh(
        path,
        "api",
        "--paginate",
        f"repos/{repo}/issues/{number}/events",
        "--jq",
        jq,
        binary=binary,
    )
    if proc.returncode != 0:
        return None
    times = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return times[-1] if times else None
