"""The merge gate: read a pull request, judge it, and merge it fail-closed.

`pr_detail` is a *view*; `merge_pr` is an independent enforcement point that
re-reads state and re-evaluates every predicate immediately before merging.
A caller must never be able to widen the gate by passing a stale payload.
"""

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from github_checker.actions import ActionResult, result_for
from github_checker.ghcli import repo_slug, run_gh
from github_checker.models import (
    ChangedFile,
    CheckRun,
    GateResult,
    PrDetail,
    ReviewThread,
)

PR_VIEW_FIELDS = (
    "number,title,url,state,isDraft,mergeable,mergeStateStatus,"
    "headRefName,headRefOid,baseRefName,reviewDecision,statusCheckRollup,"
    "files,changedFiles"
)

_SUCCESSFUL_CHECKS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})

# gh's embedded PR query asks for `contexts(first:100)` and the --json
# projection is a flat list with no pageInfo, so the count is the only signal
# we have that the rollup may be short. Exactly 100 green checks refuses
# needlessly; that errs in the direction this gate exists to err in.
CHECKS_ROLLUP_CAP = 100


def _check_state(item: dict[str, Any]) -> str:
    """Flatten a check run or a legacy status context to one state word."""
    if item.get("__typename") == "StatusContext" or "context" in item:
        return str(item.get("state") or "PENDING")
    if item.get("status") != "COMPLETED":
        return "PENDING"
    return str(item.get("conclusion") or "PENDING")


def _check_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("context") or "?")


def parse_pr_view(data: dict[str, Any], *, file_limit: int) -> PrDetail:
    """Map `gh pr view --json PR_VIEW_FIELDS` output onto a PrDetail."""
    rollup = data.get("statusCheckRollup") or []
    raw_files = data.get("files") or []
    files = [
        ChangedFile(
            path=item["path"],
            additions=item.get("additions", 0),
            deletions=item.get("deletions", 0),
        )
        for item in raw_files[:file_limit]
    ]
    total = data.get("changedFiles") or len(raw_files)
    return PrDetail(
        number=data["number"],
        title=data.get("title", ""),
        url=data.get("url", ""),
        state=data.get("state", "UNKNOWN"),
        is_draft=bool(data.get("isDraft")),
        mergeable=data.get("mergeable") or "UNKNOWN",
        merge_state_status=data.get("mergeStateStatus"),
        head_branch=data.get("headRefName", ""),
        head_sha=data.get("headRefOid", ""),
        base_branch=data.get("baseRefName", ""),
        review_decision=data.get("reviewDecision") or None,
        checks=[
            CheckRun(name=_check_name(item), state=_check_state(item))
            for item in rollup
        ],
        checks_truncated=len(rollup) >= CHECKS_ROLLUP_CAP,
        files=files,
        files_total=total,
        files_truncated=len(raw_files) > file_limit or total > len(files),
    )


class GateUnavailable(Exception):
    """PR state could not be established; the gate must stay closed."""


THREAD_PAGE_SIZE = 100
MAX_THREAD_PAGES = 5
_EXCERPT_CHARS = 280

_THREADS_QUERY = (
    """
query($owner:String!,$name:String!,$number:Int!,$cursor:String){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      reviewThreads(first:%d, after:$cursor){
        pageInfo{ hasNextPage endCursor }
        nodes{
          id isResolved isOutdated path
          comments(first:1){ nodes{ author{ login } body } }
        }
      }
    }
  }
}
"""
    % THREAD_PAGE_SIZE
)


def _review_threads_connection(page: dict[str, Any]) -> dict[str, Any]:
    """The `reviewThreads` connection, or raise if it isn't positively there.

    A GraphQL error, or a break anywhere in data -> repository ->
    pullRequest -> reviewThreads, must not be read as "zero threads" — that
    is the empty-list-opens-the-gate inversion this verb exists to prevent.
    An actually-present connection (even with `nodes: []`) is a legitimate
    empty result and is returned as-is.
    """
    if "errors" in page:
        raise GateUnavailable(
            f"review-threads response carried errors: {page['errors']!r}"
        )
    data = page.get("data")
    if not isinstance(data, dict):
        raise GateUnavailable("review-threads response missing 'data'")
    repository = data.get("repository")
    if not isinstance(repository, dict):
        raise GateUnavailable("review-threads response missing 'repository'")
    pull_request = repository.get("pullRequest")
    if not isinstance(pull_request, dict):
        raise GateUnavailable("review-threads response missing 'pullRequest'")
    connection = pull_request.get("reviewThreads")
    if not isinstance(connection, dict):
        raise GateUnavailable("review-threads response missing 'reviewThreads'")
    return connection


def parse_review_threads(page: dict[str, Any]) -> tuple[list[ReviewThread], str | None]:
    """Map one GraphQL page to threads plus the next cursor (None if last)."""
    connection = _review_threads_connection(page)
    # a present connection still has to positively carry both sub-fields:
    # `or []` / `or {}` here would turn an unreadable page back into the
    # "zero threads, read complete" answer the connection guard just refused
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        raise GateUnavailable("review-threads connection missing 'nodes'")
    info = connection.get("pageInfo")
    if not isinstance(info, dict):
        raise GateUnavailable("review-threads connection missing 'pageInfo'")
    threads: list[ReviewThread] = []
    for node in nodes:
        comments = (node.get("comments") or {}).get("nodes") or []
        first = comments[0] if comments else {}
        body = first.get("body")
        threads.append(
            ReviewThread(
                id=node["id"],
                is_resolved=bool(node.get("isResolved")),
                is_outdated=bool(node.get("isOutdated")),
                path=node.get("path"),
                author=(first.get("author") or {}).get("login"),
                excerpt=body[:_EXCERPT_CHARS] if body else None,
            )
        )
    cursor = info.get("endCursor") if info.get("hasNextPage") else None
    return threads, cursor


def fetch_review_threads(
    path: Path, owner: str, name: str, number: int, *, binary: str = "gh"
) -> tuple[list[ReviewThread], bool]:
    """All review threads of a PR; the flag means the page cap cut the list."""
    threads: list[ReviewThread] = []
    cursor: str | None = None
    for _ in range(MAX_THREAD_PAGES):
        args = [
            "api",
            "graphql",
            "-f",
            f"query={_THREADS_QUERY}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={number}",
        ]
        if cursor is not None:
            args += ["-F", f"cursor={cursor}"]
        proc = run_gh(path, *args, binary=binary)
        if proc.returncode != 0:
            # неизвестное состояние тредов = закрытые ворота, а не пустой список
            raise GateUnavailable(proc.stderr.strip() or "gh api graphql failed")
        try:
            page = json.loads(proc.stdout)
        except json.JSONDecodeError as err:
            raise GateUnavailable("unexpected non-JSON from gh api graphql") from err
        batch, cursor = parse_review_threads(page)
        threads.extend(batch)
        if cursor is None:
            return threads, False
    return threads, True


def evaluate_gate(detail: PrDetail) -> GateResult:
    """Judge a PR against every merge predicate; report all failures at once.

    `detail.merge_state_status` is deliberately **not** consulted, though it is
    fetched and shown by `pr-detail`. Gating on it would also refuse `BEHIND`
    and `UNSTABLE` — a merge-policy decision (does this tool refuse a PR that
    is merely behind its base?) that belongs to the repo owner, not here.
    """
    failed: list[str] = []
    if detail.state != "OPEN":
        failed.append("open")
    if detail.is_draft:
        failed.append("not-draft")
    if detail.mergeable != "MERGEABLE":
        failed.append("mergeable")
    if any(check.state not in _SUCCESSFUL_CHECKS for check in detail.checks):
        failed.append("checks-green")
    if detail.checks_truncated:
        # усечённый rollup нельзя читать как "все чеки зелёные" — тот же
        # инвариант, что и threads-complete, только для списка чеков
        failed.append("checks-complete")
    if detail.review_decision not in (None, "APPROVED"):
        # allowlist, не blocklist: GitHub's reviewDecision enum is documented as
        # extensible, и незнакомое значение должно блокировать, а не проходить
        failed.append("approvals")
    if any(not thread.is_resolved for thread in detail.review_threads):
        failed.append("threads-resolved")
    if detail.threads_truncated:
        # неполный список тредов не должен читаться как "нет открытых тредов"
        failed.append("threads-complete")
    if detail.allows_squash is not True:
        failed.append("squash-allowed")
    return GateResult(passed=not failed, failed=failed)


FILE_LIMIT = 100
DIFF_LINE_LIMIT = 2000
DIFF_BYTE_LIMIT = 200_000


def truncate_diff(text: str, *, line_limit: int, byte_limit: int) -> tuple[str, bool]:
    """Cut a diff at whichever budget binds first; report whether it was cut."""
    lines = text.splitlines(keepends=True)
    cut = len(lines) > line_limit
    kept = lines[:line_limit]
    out = "".join(kept)
    encoded = out.encode()
    if len(encoded) > byte_limit:
        out = encoded[:byte_limit].decode(errors="ignore")
        cut = True
    return out, cut


def fetch_allows_squash(
    path: Path, owner: str, name: str, *, binary: str = "gh"
) -> bool | None:
    """Whether branch protection/settings permit a squash merge; None if unknown."""
    proc = run_gh(
        path,
        "api",
        f"repos/{owner}/{name}",
        "--jq",
        ".allow_squash_merge",
        binary=binary,
    )
    if proc.returncode != 0:
        return None
    answer = proc.stdout.strip()
    if answer == "true":
        return True
    if answer == "false":
        return False
    return None


def pr_detail(
    path: Path,
    number: int,
    *,
    file_limit: int = FILE_LIMIT,
    diff_line_limit: int = DIFF_LINE_LIMIT,
    diff_byte_limit: int = DIFF_BYTE_LIMIT,
    binary: str = "gh",
) -> PrDetail:
    """Read one pull request: state, checks, files, diff and review threads."""
    slug = repo_slug(path, binary=binary)
    if slug is None:
        raise GateUnavailable("cannot resolve owner/repo for this clone")
    owner, name = slug

    view = run_gh(
        path, "pr", "view", str(number), "--json", PR_VIEW_FIELDS, binary=binary
    )
    if view.returncode != 0:
        raise GateUnavailable(view.stderr.strip() or "gh pr view failed")
    try:
        data = json.loads(view.stdout)
    except json.JSONDecodeError as err:
        raise GateUnavailable("unexpected non-JSON from gh pr view") from err

    try:
        # rc 0 with the wrong shape is still "state that cannot be
        # established" — data["number"], item["path"] and node["id"] index
        # directly, and a mis-shaped-but-valid-JSON body must close the gate
        # via GateUnavailable rather than let the raw exception escape the
        # verb. AttributeError is in the tuple because a list whose *items*
        # are the wrong type reaches item.get(...) / node.get(...).
        detail = parse_pr_view(data, file_limit=file_limit)
        detail.review_threads, detail.threads_truncated = fetch_review_threads(
            path, owner, name, number, binary=binary
        )
    except (AttributeError, KeyError, TypeError, ValidationError) as err:
        raise GateUnavailable(f"unexpected PR payload shape: {err!r}") from err
    detail.allows_squash = fetch_allows_squash(path, owner, name, binary=binary)

    diff = run_gh(path, "pr", "diff", str(number), binary=binary)
    if diff.returncode == 0:
        detail.diff, detail.diff_truncated = truncate_diff(
            diff.stdout, line_limit=diff_line_limit, byte_limit=diff_byte_limit
        )
    return detail


def _merge_failure(
    path: Path,
    error: str,
    *,
    merged: bool | None = False,
    gate_failed: list[str] | None = None,
    detail: PrDetail | None = None,
) -> ActionResult:
    """One shape for every `merge` refusal, so no field can go missing.

    `merged` defaults to `False` because every *pre-launch* refusal knows
    for certain that nothing was merged. The one caller that runs after
    `gh pr merge` has already been launched must pass `merged=None`
    instead: a non-zero exit there does not prove the merge did not
    happen. `--delete-branch` runs *after* the merge (gh's own wording),
    so a protected branch, a permissions problem or a dropped connection
    exits non-zero with the pull request already squashed.
    """
    return result_for(
        "merge",
        path,
        ok=False,
        merged=merged,
        local_sync="not_attempted",
        gate_failed=gate_failed,
        error=error,
        pr_detail=detail,
    )


def merge_pr(
    path: Path, number: int, *, if_head: str, binary: str = "gh"
) -> ActionResult:
    """Squash-merge a PR only if every predicate still holds at this moment."""
    try:
        detail = pr_detail(path, number, binary=binary)
    except GateUnavailable as err:
        return _merge_failure(path, str(err))

    gate = evaluate_gate(detail)
    failed = list(gate.failed)
    if detail.head_sha != if_head:
        # содержимое PR изменилось после того, как оператор его увидел
        failed.append("head-sha")
    if failed:
        return _merge_failure(
            path,
            "merge gate refused: " + ", ".join(failed),
            gate_failed=failed,
            detail=detail,
        )

    proc = run_gh(
        path,
        "pr",
        "merge",
        str(number),
        "--squash",
        "--delete-branch",
        # Server-side TOCTOU enforcement. The `head_sha` comparison above is
        # a local read and cannot bind: a push landing between that read and
        # this call would merge something the operator never saw. GitHub
        # rejects the merge itself when the head has moved, which is the only
        # place the check can be authoritative.
        "--match-head-commit",
        if_head,
        binary=binary,
    )
    if proc.returncode != 0:
        # Past the point of no return: `gh` has been launched, so a non-zero
        # exit does not prove nothing happened. `merged=None` — unknown.
        return _merge_failure(
            path, proc.stderr.strip() or "gh pr merge failed", merged=None
        )
    return result_for(
        "merge",
        path,
        ok=True,
        merged=True,
        # `merge` never touches the local clone; say so rather than leaving
        # the field null only on the one path that succeeded
        local_sync="not_attempted",
        detail=f"pull request #{number} squash-merged",
        pr_url=detail.url,
    )
