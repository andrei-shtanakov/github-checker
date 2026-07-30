# Merge-gate verbs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add three headless verbs — `pr-detail` (read), `merge` (fail-closed mutation), `post-merge-sync` (local plumbing) — so a caller can drive a pull request from "open" to "merged and locally synced" without touching the GitHub API itself.

**Architecture:** `pr-detail` gathers PR state via `gh pr view --json` plus a GraphQL query for review threads (resolved state is GraphQL-only), and returns it with explicit truncation flags. `merge` does **not** trust that payload: it re-fetches state and re-evaluates every gate predicate immediately before merging, refusing on any unmet predicate. `post-merge-sync` is pure local git plumbing built on the existing `localgit` helpers, fail-closed on anything that could lose work. All three print one JSON `ActionResult` and exit 1 on failure, exactly like the existing `pull` / `open-pr` / `propose-pr` verbs.

**Tech Stack:** Python 3.11+, pydantic v2, `gh` CLI (REST via `gh pr view`, GraphQL via `gh api graphql`), `git` CLI via `subprocess`, pytest, uv.

**Design source:** `_cowork_output/2026-07-30-dispatcher-operator-console-design.md` §5.3 (S1 slice). That file lives in the dev-only cowork workspace and is **not** readable from shipped code — everything needed is restated here.

## Global Constraints

- Package management: **uv only**. `uv add <pkg>`, `uv run pytest`, `uv run ruff`. Never `pip`, never `uv pip install`.
- Line length **88** chars. `uv run ruff format .` then `uv run ruff check . --fix` before every commit.
- Type hints required on all functions. Public functions need docstrings.
- **Headless JSON contract (non-negotiable):** every verb prints exactly one JSON `ActionResult` to stdout and exits 1 when `ok=false`. Never let argparse `exit(2)` with a usage message replace it — follow the comment pattern at `github_checker/main.py:129-133` (validate inside the function, return `ActionResult(ok=False, ...)`).
- **Never raise out of a verb.** A missing `gh` binary, a timeout, or malformed JSON becomes a failed `ActionResult`, mirroring `github_checker/actions.py:39-52`.
- **Fail-closed:** any predicate that cannot be positively confirmed (including GitHub's `UNKNOWN` mergeability) blocks the merge.
- Existing tests must stay green: `uv run pytest` from the repo root.
- Comments in this codebase are sparse and explain *why*, not *what*; Russian comments appear where they explain a non-obvious guard. Match that density — do not add narration.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `github_checker/ghcli.py` | **new** — synchronous `gh` invocation for a local clone (`run_gh`) and `repo_slug()`. Extracted so `actions.py` and `prgate.py` share one hardened wrapper. |
| `github_checker/models.py` | **modify** — add `ReviewThread`, `CheckRun`, `ChangedFile`, `PrDetail`, `GateResult`. |
| `github_checker/prgate.py` | **new** — the GitHub side: `pr_detail()`, `evaluate_gate()`, `merge_pr()`. |
| `github_checker/localgit.py` | **modify** — add `switch_branch`, `worktree_holding`, `merged_local_branches`, `delete_branch`, `has_upstream`, `is_detached`. |
| `github_checker/actions.py` | **modify** — `ActionResult` gains `merged` / `local_sync` / `gate_failed` / `pr_detail`; add `post_merge_sync()`. |
| `github_checker/main.py` | **modify** — three subcommands wired to the above. |
| `tests/test_ghcli.py` | **new** — `run_gh` / `repo_slug` failure modes. |
| `tests/test_prgate_parse.py` | **new** — `gh` JSON + GraphQL → `PrDetail`, truncation flags. |
| `tests/test_prgate_gate.py` | **new** — the seven predicates as a pure function. |
| `tests/test_prgate_merge.py` | **new** — `merge_pr()` re-check, TOCTOU, no-mutation-on-refusal. |
| `tests/test_post_merge_sync.py` | **new** — real temp git repos. |
| `tests/test_main.py` | **modify** — CLI wiring and exit codes for the three verbs. |

**Why `prgate.py` and not more of `actions.py`:** `actions.py` is the local/simple whitelist (`pull`, `open-pr`); the gate carries seven predicates, two API shapes and truncation rules. Keeping it separate mirrors the existing split where `propose.py` and `protection.py` each own a subsystem.

---

### Task 1: Shared `gh` invocation helper

**Files:**
- Create: `github_checker/ghcli.py`
- Modify: `github_checker/actions.py:39-52` (replace the private `_gh` with an import)
- Test: `tests/test_ghcli.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `run_gh(path: Path, *args: str) -> subprocess.CompletedProcess[str]` — never raises; a missing binary or timeout comes back as `returncode=127`. `repo_slug(path: Path) -> tuple[str, str] | None` — `(owner, name)` or `None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ghcli.py
"""Shared gh invocation wrapper: never raises, resolves owner/name."""

import json
from pathlib import Path

from github_checker.ghcli import repo_slug, run_gh


def test_run_gh_missing_binary_becomes_result(tmp_path: Path) -> None:
    proc = run_gh(tmp_path, "pr", "view", binary="definitely-not-a-real-binary")
    assert proc.returncode == 127
    assert proc.stdout == ""
    assert "definitely-not-a-real-binary" in proc.stderr


def _fake_gh(tmp_path: Path, stdout: str, code: int = 0) -> str:
    script = tmp_path / "fake_gh.py"
    script.write_text(
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.exit({code})\n"
    )
    launcher = tmp_path / "fake_gh"
    launcher.write_text(f"#!/bin/sh\nexec python3 {script} \"$@\"\n")
    launcher.chmod(0o755)
    return str(launcher)


def test_repo_slug_parses_owner_and_name(tmp_path: Path) -> None:
    payload = json.dumps({"owner": {"login": "acme"}, "name": "widget"})
    slug = repo_slug(tmp_path, binary=_fake_gh(tmp_path, payload))
    assert slug == ("acme", "widget")


def test_repo_slug_returns_none_when_gh_fails(tmp_path: Path) -> None:
    assert repo_slug(tmp_path, binary=_fake_gh(tmp_path, "", code=1)) is None


def test_repo_slug_returns_none_on_garbage_output(tmp_path: Path) -> None:
    assert repo_slug(tmp_path, binary=_fake_gh(tmp_path, "not json")) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_ghcli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'github_checker.ghcli'`

- [ ] **Step 3: Write minimal implementation**

```python
# github_checker/ghcli.py
"""Synchronous `gh` invocation against a local clone.

Shared by the whitelist actions and the merge gate so both get the same
hardening: a missing binary or a timeout is a failed process, never an
exception that would escape a verb and break the JSON contract.
"""

import json
import subprocess
from pathlib import Path

GH_TIMEOUT = 60


def run_gh(
    path: Path, *args: str, binary: str = "gh", timeout: int = GH_TIMEOUT
) -> subprocess.CompletedProcess[str]:
    """Run gh in *path*; never raises — failures surface as returncode 127."""
    try:
        return subprocess.run(
            [binary, *args],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as err:
        return subprocess.CompletedProcess(
            [binary, *args], returncode=127, stdout="", stderr=str(err)
        )


def repo_slug(path: Path, *, binary: str = "gh") -> tuple[str, str] | None:
    """`(owner, name)` of the clone's GitHub repo, or None if unresolvable."""
    proc = run_gh(path, "repo", "view", "--json", "owner,name", binary=binary)
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
        return data["owner"]["login"], data["name"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_ghcli.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Point `actions.py` at the shared helper**

Delete the private `_gh` function in `github_checker/actions.py` (currently lines 39-52) and replace its two call sites (`_gh(path, ...)` inside `open_pr`) with `run_gh(path, ...)`. Add to the imports:

```python
from github_checker.ghcli import run_gh
```

Remove the now-unused `subprocess` import from `actions.py` if nothing else in the file uses it.

- [ ] **Step 6: Run the whole suite — the refactor must be invisible**

Run: `uv run pytest -q`
Expected: PASS, same count as before the change (plus the 4 new ones)

- [ ] **Step 7: Format, lint, commit**

```bash
uv run ruff format . && uv run ruff check . --fix
git add github_checker/ghcli.py github_checker/actions.py tests/test_ghcli.py
git commit -m "refactor: extract shared gh invocation wrapper into ghcli"
```

---

### Task 2: `PrDetail` models and `gh pr view` parsing

**Files:**
- Modify: `github_checker/models.py` (append after `LocalStatus`)
- Create: `github_checker/prgate.py`
- Test: `tests/test_prgate_parse.py`

**Interfaces:**
- Consumes: `run_gh` from Task 1.
- Produces: models `ChangedFile`, `CheckRun`, `ReviewThread`, `PrDetail`; and
  `parse_pr_view(data: dict[str, Any], *, file_limit: int) -> PrDetail` — maps
  `gh pr view --json` output to a `PrDetail` with `review_threads=[]` (Task 3 fills them).

**Field reference — exactly what `gh pr view --json` is asked for:**
`number,title,url,state,isDraft,mergeable,mergeStateStatus,headRefName,headRefOid,baseRefName,reviewDecision,statusCheckRollup,files,changedFiles`

`mergeable` is one of `MERGEABLE` / `CONFLICTING` / `UNKNOWN` (GitHub computes it lazily — `UNKNOWN` must never be read as "fine"). `statusCheckRollup` is a list whose items are either check runs (`__typename: "CheckRun"`, with `status` and `conclusion`) or legacy commit statuses (`__typename: "StatusContext"`, with `state`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prgate_parse.py
"""gh pr view JSON -> PrDetail, including truncation of large file lists."""

from github_checker.prgate import parse_pr_view


def _view(**overrides: object) -> dict:
    data = {
        "number": 7,
        "title": "Add widget",
        "url": "https://github.com/acme/widget/pull/7",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefName": "feat/widget",
        "headRefOid": "a" * 40,
        "baseRefName": "master",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "name": "tests",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
        ],
        "files": [{"path": "a.py", "additions": 3, "deletions": 1}],
        "changedFiles": 1,
    }
    data.update(overrides)
    return data


def test_parse_maps_core_fields() -> None:
    detail = parse_pr_view(_view(), file_limit=100)
    assert detail.number == 7
    assert detail.head_sha == "a" * 40
    assert detail.base_branch == "master"
    assert detail.is_draft is False
    assert detail.mergeable == "MERGEABLE"
    assert detail.review_decision == "APPROVED"
    assert detail.review_threads == []


def test_parse_normalises_check_runs_and_status_contexts() -> None:
    detail = parse_pr_view(
        _view(
            statusCheckRollup=[
                {
                    "__typename": "CheckRun",
                    "name": "tests",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                },
                {"__typename": "StatusContext", "context": "ci", "state": "SUCCESS"},
                {
                    "__typename": "CheckRun",
                    "name": "slow",
                    "status": "IN_PROGRESS",
                    "conclusion": None,
                },
            ]
        ),
        file_limit=100,
    )
    assert [(c.name, c.state) for c in detail.checks] == [
        ("tests", "FAILURE"),
        ("ci", "SUCCESS"),
        ("slow", "PENDING"),
    ]


def test_parse_truncates_file_list_but_keeps_the_real_total() -> None:
    files = [{"path": f"f{i}.py", "additions": 1, "deletions": 0} for i in range(150)]
    detail = parse_pr_view(_view(files=files, changedFiles=150), file_limit=100)
    assert len(detail.files) == 100
    assert detail.files_total == 150
    assert detail.files_truncated is True


def test_parse_does_not_flag_truncation_when_everything_fits() -> None:
    detail = parse_pr_view(_view(), file_limit=100)
    assert detail.files_truncated is False
    assert detail.files_total == 1


def test_parse_tolerates_missing_optional_blocks() -> None:
    detail = parse_pr_view(
        _view(statusCheckRollup=None, files=None, reviewDecision=None), file_limit=100
    )
    assert detail.checks == []
    assert detail.files == []
    assert detail.review_decision is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prgate_parse.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'github_checker.prgate'`

- [ ] **Step 3: Add the models**

Append to `github_checker/models.py`:

```python
class ChangedFile(BaseModel):
    """One file touched by a pull request."""

    path: str
    additions: int = 0
    deletions: int = 0


class CheckRun(BaseModel):
    """A normalised CI signal: check run and legacy status look the same here."""

    name: str
    state: str  # SUCCESS | FAILURE | PENDING | SKIPPED | ...


class ReviewThread(BaseModel):
    """A review conversation and whether it is still open."""

    id: str
    is_resolved: bool
    is_outdated: bool = False
    path: str | None = None
    author: str | None = None
    excerpt: str | None = None


class PrDetail(BaseModel):
    """Everything the merge gate reads about one pull request."""

    number: int
    title: str
    url: str
    state: str
    is_draft: bool
    mergeable: str
    merge_state_status: str | None = None
    head_branch: str
    head_sha: str
    base_branch: str
    review_decision: str | None = None
    checks: list[CheckRun] = []
    files: list[ChangedFile] = []
    files_total: int = 0
    files_truncated: bool = False
    review_threads: list[ReviewThread] = []
    threads_truncated: bool = False
    diff: str | None = None
    diff_truncated: bool = False
    allows_squash: bool | None = None
```

- [ ] **Step 4: Write the parser**

```python
# github_checker/prgate.py
"""The merge gate: read a pull request, judge it, and merge it fail-closed.

`pr_detail` is a *view*; `merge_pr` is an independent enforcement point that
re-reads state and re-evaluates every predicate immediately before merging.
A caller must never be able to widen the gate by passing a stale payload.
"""

from typing import Any

from github_checker.models import (
    ChangedFile,
    CheckRun,
    PrDetail,
)

PR_VIEW_FIELDS = (
    "number,title,url,state,isDraft,mergeable,mergeStateStatus,"
    "headRefName,headRefOid,baseRefName,reviewDecision,statusCheckRollup,"
    "files,changedFiles"
)

_SUCCESSFUL_CHECKS = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})


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
        files=files,
        files_total=total,
        files_truncated=len(raw_files) > file_limit or total > len(files),
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_prgate_parse.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Format, lint, commit**

```bash
uv run ruff format . && uv run ruff check . --fix
git add github_checker/models.py github_checker/prgate.py tests/test_prgate_parse.py
git commit -m "feat(prgate): PrDetail models and gh pr view parsing"
```

---

### Task 3: Review threads via GraphQL, with explicit truncation

**Files:**
- Modify: `github_checker/prgate.py`
- Test: `tests/test_prgate_parse.py` (append)

**Interfaces:**
- Consumes: `ReviewThread` (Task 2), `run_gh` / `repo_slug` (Task 1).
- Produces: `parse_review_threads(page: dict[str, Any]) -> tuple[list[ReviewThread], str | None]`
  — threads plus the next cursor (`None` when exhausted); and
  `fetch_review_threads(path, owner, name, number, *, binary="gh") -> tuple[list[ReviewThread], bool]`
  — all threads plus a `truncated` flag.

**Why GraphQL:** `gh pr view --json` has no `reviewThreads` field, and the REST
comments endpoint does not expose resolution state. `isResolved` exists only on
the GraphQL `PullRequest.reviewThreads` connection. This is the single reason the
verb makes two API calls.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_prgate_parse.py
from github_checker.prgate import parse_review_threads


def _page(nodes: list[dict], has_next: bool = False, cursor: str = "c1") -> dict:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                        "nodes": nodes,
                    }
                }
            }
        }
    }


def _thread(tid: str, resolved: bool) -> dict:
    return {
        "id": tid,
        "isResolved": resolved,
        "isOutdated": False,
        "path": "a.py",
        "comments": {
            "nodes": [{"author": {"login": "reviewer"}, "body": "please fix this"}]
        },
    }


def test_parse_threads_reads_resolution_and_author() -> None:
    threads, cursor = parse_review_threads(_page([_thread("t1", False)]))
    assert cursor is None
    assert threads[0].id == "t1"
    assert threads[0].is_resolved is False
    assert threads[0].author == "reviewer"
    assert threads[0].excerpt == "please fix this"


def test_parse_threads_returns_cursor_when_more_pages_exist() -> None:
    _, cursor = parse_review_threads(_page([_thread("t1", True)], has_next=True))
    assert cursor == "c1"


def test_parse_threads_survives_a_thread_with_no_comments() -> None:
    node = _thread("t2", True)
    node["comments"] = {"nodes": []}
    threads, _ = parse_review_threads(_page([node]))
    assert threads[0].author is None
    assert threads[0].excerpt is None


def test_parse_threads_handles_an_empty_connection() -> None:
    threads, cursor = parse_review_threads(_page([]))
    assert threads == []
    assert cursor is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prgate_parse.py -k threads -v`
Expected: FAIL — `ImportError: cannot import name 'parse_review_threads'`

- [ ] **Step 3: Write the implementation**

Append to `github_checker/prgate.py`:

```python
import json
from pathlib import Path

from github_checker.ghcli import run_gh
from github_checker.models import ReviewThread


class GateUnavailable(Exception):
    """PR state could not be established; the gate must stay closed."""


THREAD_PAGE_SIZE = 100
MAX_THREAD_PAGES = 5
_EXCERPT_CHARS = 280

_THREADS_QUERY = """
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
""" % THREAD_PAGE_SIZE


def parse_review_threads(page: dict[str, Any]) -> tuple[list[ReviewThread], str | None]:
    """Map one GraphQL page to threads plus the next cursor (None if last)."""
    connection = (
        page.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
    )
    threads: list[ReviewThread] = []
    for node in connection.get("nodes") or []:
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
    info = connection.get("pageInfo") or {}
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
```

`GateUnavailable` is declared above `fetch_review_threads` (in the same block) on
purpose: a failed or unparseable threads query must raise, never return `[]`.
An empty list would read as "no unresolved threads" and silently open the gate —
the exact inversion this design forbids.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_prgate_parse.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && uv run ruff check . --fix
git add github_checker/prgate.py tests/test_prgate_parse.py
git commit -m "feat(prgate): review threads via GraphQL with page-cap truncation"
```

---

### Task 4: The gate — seven predicates as a pure function

**Files:**
- Modify: `github_checker/prgate.py`, `github_checker/models.py`
- Test: `tests/test_prgate_gate.py`

**Interfaces:**
- Consumes: `PrDetail` (Task 2).
- Produces: `GateResult` model and `evaluate_gate(detail: PrDetail) -> GateResult`.
  `GateResult.passed: bool`, `GateResult.failed: list[str]` (predicate names, stable
  strings — the dispatcher UI shows them verbatim).

**The seven predicates, with their stable names:**

| Name | Passes when |
|------|-------------|
| `open` | `state == "OPEN"` |
| `not-draft` | `is_draft is False` |
| `mergeable` | `mergeable == "MERGEABLE"` (so `UNKNOWN` and `CONFLICTING` both fail) |
| `checks-green` | every check's state is in `{SUCCESS, NEUTRAL, SKIPPED}` |
| `approvals` | `review_decision` is `APPROVED` **or** `None` (no review required on this repo) — but never `CHANGES_REQUESTED` / `REVIEW_REQUIRED` |
| `threads-resolved` | no `ReviewThread` with `is_resolved is False` |
| `squash-allowed` | `allows_squash is True` |

Note `head_sha` is **not** a gate predicate — it is a caller-supplied guard checked
separately in Task 5, because it compares against an argument, not against state.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prgate_gate.py
"""The gate is fail-closed: anything not positively green blocks the merge."""

import pytest

from github_checker.models import CheckRun, PrDetail, ReviewThread
from github_checker.prgate import evaluate_gate


def make_detail(**overrides: object) -> PrDetail:
    data: dict = {
        "number": 7,
        "title": "t",
        "url": "u",
        "state": "OPEN",
        "is_draft": False,
        "mergeable": "MERGEABLE",
        "head_branch": "feat",
        "head_sha": "a" * 40,
        "base_branch": "master",
        "review_decision": "APPROVED",
        "checks": [CheckRun(name="tests", state="SUCCESS")],
        "review_threads": [],
        "allows_squash": True,
    }
    data.update(overrides)
    return PrDetail(**data)


def test_green_pr_passes() -> None:
    result = evaluate_gate(make_detail())
    assert result.passed is True
    assert result.failed == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"state": "CLOSED"}, "open"),
        ({"is_draft": True}, "not-draft"),
        ({"mergeable": "CONFLICTING"}, "mergeable"),
        ({"mergeable": "UNKNOWN"}, "mergeable"),
        ({"checks": [CheckRun(name="t", state="FAILURE")]}, "checks-green"),
        ({"checks": [CheckRun(name="t", state="PENDING")]}, "checks-green"),
        ({"review_decision": "CHANGES_REQUESTED"}, "approvals"),
        ({"review_decision": "REVIEW_REQUIRED"}, "approvals"),
        ({"allows_squash": False}, "squash-allowed"),
        ({"allows_squash": None}, "squash-allowed"),
    ],
)
def test_each_predicate_blocks(overrides: dict, expected: str) -> None:
    result = evaluate_gate(make_detail(**overrides))
    assert result.passed is False
    assert expected in result.failed


def test_unresolved_thread_blocks_regardless_of_author() -> None:
    detail = make_detail(
        review_threads=[
            ReviewThread(id="t1", is_resolved=True, author="copilot-pull-request-reviewer[bot]"),
            ReviewThread(id="t2", is_resolved=False, author="a-human"),
        ]
    )
    result = evaluate_gate(detail)
    assert result.passed is False
    assert "threads-resolved" in result.failed


def test_resolved_threads_do_not_block() -> None:
    detail = make_detail(
        review_threads=[ReviewThread(id="t1", is_resolved=True, author="anyone")]
    )
    assert evaluate_gate(detail).passed is True


def test_absent_review_decision_is_allowed() -> None:
    """A repo with no required reviewers reports None, not APPROVED."""
    assert evaluate_gate(make_detail(review_decision=None)).passed is True


def test_no_checks_configured_is_allowed() -> None:
    assert evaluate_gate(make_detail(checks=[])).passed is True


def test_all_failures_are_reported_not_just_the_first() -> None:
    result = evaluate_gate(
        make_detail(is_draft=True, mergeable="UNKNOWN", allows_squash=False)
    )
    assert set(result.failed) >= {"not-draft", "mergeable", "squash-allowed"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prgate_gate.py -v`
Expected: FAIL — `ImportError: cannot import name 'evaluate_gate'`

- [ ] **Step 3: Write the implementation**

Add to `github_checker/models.py`:

```python
class GateResult(BaseModel):
    """Which merge predicates failed; empty `failed` means the gate is open."""

    passed: bool
    failed: list[str] = []
```

Add to `github_checker/prgate.py`:

```python
def evaluate_gate(detail: PrDetail) -> GateResult:
    """Judge a PR against every merge predicate; report all failures at once."""
    failed: list[str] = []
    if detail.state != "OPEN":
        failed.append("open")
    if detail.is_draft:
        failed.append("not-draft")
    if detail.mergeable != "MERGEABLE":
        failed.append("mergeable")
    if any(check.state not in _SUCCESSFUL_CHECKS for check in detail.checks):
        failed.append("checks-green")
    if detail.review_decision in ("CHANGES_REQUESTED", "REVIEW_REQUIRED"):
        failed.append("approvals")
    if any(not thread.is_resolved for thread in detail.review_threads):
        failed.append("threads-resolved")
    if detail.allows_squash is not True:
        failed.append("squash-allowed")
    return GateResult(passed=not failed, failed=failed)
```

Add `GateResult` to the `from github_checker.models import (...)` block.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_prgate_gate.py -v`
Expected: PASS (17 tests, counting the parametrised cases)

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && uv run ruff check . --fix
git add github_checker/prgate.py github_checker/models.py tests/test_prgate_gate.py
git commit -m "feat(prgate): fail-closed merge gate predicates"
```

---

### Task 5: `pr_detail()` — assemble the read verb

**Files:**
- Modify: `github_checker/prgate.py`
- Test: `tests/test_prgate_parse.py` (append), `tests/test_prgate_merge.py` (created next task)

**Interfaces:**
- Consumes: `parse_pr_view`, `fetch_review_threads`, `repo_slug`, `run_gh`.
- Produces: `pr_detail(path: Path, number: int, *, file_limit: int = 100, diff_line_limit: int = 2000, diff_byte_limit: int = 200_000, binary: str = "gh") -> PrDetail` — raises `GateUnavailable` when state cannot be established.
  Also `fetch_allows_squash(path, owner, name, *, binary="gh") -> bool | None`.

**Truncation rules (all explicit, never silent):** files → `file_limit` with
`files_total` / `files_truncated`; diff → cut at whichever of `diff_line_limit`
lines or `diff_byte_limit` bytes comes first, setting `diff_truncated`; threads →
`MAX_THREAD_PAGES` pages with `threads_truncated`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_prgate_parse.py
from pathlib import Path

from github_checker.prgate import truncate_diff


def test_truncate_diff_cuts_by_line_count() -> None:
    text = "\n".join(f"line {i}" for i in range(500))
    out, cut = truncate_diff(text, line_limit=100, byte_limit=10**9)
    assert cut is True
    assert len(out.splitlines()) == 100


def test_truncate_diff_cuts_by_byte_budget() -> None:
    text = "\n".join("x" * 100 for _ in range(500))
    out, cut = truncate_diff(text, line_limit=10**9, byte_limit=1000)
    assert cut is True
    assert len(out.encode()) <= 1000


def test_truncate_diff_leaves_small_diffs_alone() -> None:
    text = "diff --git a/a.py b/a.py\n+one line\n"
    out, cut = truncate_diff(text, line_limit=2000, byte_limit=200_000)
    assert out == text
    assert cut is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prgate_parse.py -k truncate -v`
Expected: FAIL — `ImportError: cannot import name 'truncate_diff'`

- [ ] **Step 3: Write the implementation**

Append to `github_checker/prgate.py`:

```python
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
        path, "api", f"repos/{owner}/{name}", "--jq", ".allow_squash_merge",
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

    detail = parse_pr_view(data, file_limit=file_limit)
    detail.review_threads, detail.threads_truncated = fetch_review_threads(
        path, owner, name, number, binary=binary
    )
    detail.allows_squash = fetch_allows_squash(path, owner, name, binary=binary)

    diff = run_gh(path, "pr", "diff", str(number), binary=binary)
    if diff.returncode == 0:
        detail.diff, detail.diff_truncated = truncate_diff(
            diff.stdout, line_limit=diff_line_limit, byte_limit=diff_byte_limit
        )
    return detail
```

Add `repo_slug` to the `github_checker.ghcli` import.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_prgate_parse.py -v`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && uv run ruff check . --fix
git add github_checker/prgate.py tests/test_prgate_parse.py
git commit -m "feat(prgate): pr_detail read verb with explicit truncation"
```

---

### Task 6: `merge_pr()` — independent enforcement with a head-SHA guard

**Files:**
- Modify: `github_checker/prgate.py`, `github_checker/actions.py`
- Test: `tests/test_prgate_merge.py`

**Interfaces:**
- Consumes: `pr_detail`, `evaluate_gate`, `run_gh`, `ActionResult`.
- Produces: `merge_pr(path: Path, number: int, *, if_head: str, binary: str = "gh") -> ActionResult`
  with `action="merge"`, `merged: bool`, `gate_failed: list[str]`.

**The contract that makes this safe:** `merge_pr` calls `pr_detail` itself. It never
accepts a caller-supplied `PrDetail`. Between the operator seeing the screen and
clicking, a push, a new review comment, a red check or a draft conversion can land;
only a re-read can see that. The head-SHA guard then catches the specific case where
the PR content changed under the reviewer.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_prgate_merge.py
"""merge is an enforcement point, not a confirmation of what the screen showed."""

from pathlib import Path

import pytest

from github_checker.models import CheckRun, PrDetail
from github_checker.prgate import GateUnavailable, merge_pr

HEAD = "a" * 40
OTHER = "b" * 40


def make_detail(**overrides: object) -> PrDetail:
    data: dict = {
        "number": 7,
        "title": "t",
        "url": "u",
        "state": "OPEN",
        "is_draft": False,
        "mergeable": "MERGEABLE",
        "head_branch": "feat",
        "head_sha": HEAD,
        "base_branch": "master",
        "review_decision": "APPROVED",
        "checks": [CheckRun(name="tests", state="SUCCESS")],
        "review_threads": [],
        "allows_squash": True,
    }
    data.update(overrides)
    return PrDetail(**data)


class Recorder:
    """Stands in for run_gh; records every invocation."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def __call__(self, path, *args, **kwargs):
        import subprocess

        self.calls.append(args)
        return subprocess.CompletedProcess(
            list(args), self.returncode, self.stdout, self.stderr
        )


@pytest.fixture
def patched(monkeypatch):
    def apply(detail: PrDetail, recorder: Recorder) -> None:
        monkeypatch.setattr(
            "github_checker.prgate.pr_detail", lambda *a, **k: detail
        )
        monkeypatch.setattr("github_checker.prgate.run_gh", recorder)

    return apply


def test_green_pr_merges_with_squash_and_delete_branch(patched) -> None:
    rec = Recorder(stdout="merged")
    patched(make_detail(), rec)
    result = merge_pr(Path("/repo"), 7, if_head=HEAD)
    assert result.ok is True
    assert result.merged is True
    assert rec.calls, "merge must actually shell out to gh"
    argv = rec.calls[0]
    assert argv[:3] == ("pr", "merge", "7")
    assert "--squash" in argv and "--delete-branch" in argv


def test_head_sha_mismatch_refuses_without_calling_gh(patched) -> None:
    """TOCTOU: someone pushed between pr-detail and the click."""
    rec = Recorder()
    patched(make_detail(head_sha=OTHER), rec)
    result = merge_pr(Path("/repo"), 7, if_head=HEAD)
    assert result.ok is False
    assert result.merged is False
    assert "head-sha" in result.gate_failed
    assert rec.calls == [], "no mutation may be attempted on a stale head"


def test_unresolved_thread_refuses_without_calling_gh(patched) -> None:
    from github_checker.models import ReviewThread

    rec = Recorder()
    patched(
        make_detail(review_threads=[ReviewThread(id="t", is_resolved=False)]), rec
    )
    result = merge_pr(Path("/repo"), 7, if_head=HEAD)
    assert result.ok is False
    assert "threads-resolved" in result.gate_failed
    assert rec.calls == []


def test_draft_refuses_even_when_checks_are_green(patched) -> None:
    rec = Recorder()
    patched(make_detail(is_draft=True), rec)
    result = merge_pr(Path("/repo"), 7, if_head=HEAD)
    assert result.ok is False
    assert "not-draft" in result.gate_failed
    assert rec.calls == []


def test_unavailable_state_refuses(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise GateUnavailable("gh api graphql failed")

    monkeypatch.setattr("github_checker.prgate.pr_detail", boom)
    result = merge_pr(Path("/repo"), 7, if_head=HEAD)
    assert result.ok is False
    assert result.merged is False
    assert "gh api graphql failed" in (result.error or "")


def test_gh_merge_failure_reports_and_does_not_claim_merged(patched) -> None:
    rec = Recorder(returncode=1, stderr="Protected branch update failed")
    patched(make_detail(), rec)
    result = merge_pr(Path("/repo"), 7, if_head=HEAD)
    assert result.ok is False
    assert result.merged is False
    assert "Protected branch" in (result.error or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_prgate_merge.py -v`
Expected: FAIL — `ImportError: cannot import name 'merge_pr'`

- [ ] **Step 3: Extend `ActionResult`**

In `github_checker/actions.py`, add these fields to `ActionResult` (after `changed_paths`):

```python
    merged: bool | None = None
    local_sync: str | None = None  # ok | failed | not_attempted | not_applicable
    gate_failed: list[str] | None = None
    pr_detail: PrDetail | None = None
```

and import `PrDetail` from `github_checker.models`.

- [ ] **Step 4: Write `merge_pr`**

Append to `github_checker/prgate.py`:

```python
from github_checker.actions import ActionResult


def merge_pr(
    path: Path, number: int, *, if_head: str, binary: str = "gh"
) -> ActionResult:
    """Squash-merge a PR only if every predicate still holds at this moment."""
    try:
        detail = pr_detail(path, number, binary=binary)
    except GateUnavailable as err:
        return ActionResult(
            action="merge",
            dir=str(path),
            ok=False,
            merged=False,
            local_sync="not_attempted",
            error=str(err),
        )

    gate = evaluate_gate(detail)
    failed = list(gate.failed)
    if detail.head_sha != if_head:
        # содержимое PR изменилось после того, как оператор его увидел
        failed.append("head-sha")
    if failed:
        return ActionResult(
            action="merge",
            dir=str(path),
            ok=False,
            merged=False,
            local_sync="not_attempted",
            gate_failed=failed,
            error="merge gate refused: " + ", ".join(failed),
            pr_detail=detail,
        )

    proc = run_gh(
        path,
        "pr",
        "merge",
        str(number),
        "--squash",
        "--delete-branch",
        binary=binary,
    )
    if proc.returncode != 0:
        return ActionResult(
            action="merge",
            dir=str(path),
            ok=False,
            merged=False,
            local_sync="not_attempted",
            error=proc.stderr.strip() or "gh pr merge failed",
        )
    return ActionResult(
        action="merge",
        dir=str(path),
        ok=True,
        merged=True,
        detail=f"pull request #{number} squash-merged",
        pr_url=detail.url,
    )
```

**Import note:** `actions.py` must not import `prgate` — the dependency runs one way
(`prgate` → `actions`) to keep it acyclic. `post_merge_sync` in Task 7 lives in
`actions.py` and needs nothing from `prgate`.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_prgate_merge.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
uv run ruff format . && uv run ruff check . --fix
git add github_checker/prgate.py github_checker/actions.py tests/test_prgate_merge.py
git commit -m "feat(prgate): merge verb re-checks the gate and guards head sha"
```

---

### Task 7: Safe local git helpers for post-merge sync

**Files:**
- Modify: `github_checker/localgit.py`
- Test: `tests/test_localgit.py` (append)

**Interfaces:**
- Consumes: the existing `_git` / `LocalGitError` in `localgit.py`.
- Produces: `is_detached(path) -> bool`, `has_upstream(path, branch) -> bool`,
  `worktree_holding(path, branch) -> str | None`, `switch_branch(path, branch) -> None`,
  `merged_local_branches(path, base) -> list[str]`, `delete_branch(path, branch) -> None`.

**Why `worktree_holding` matters here:** this workspace routinely has a second
worktree checked out on a branch (`git worktree list` shows it). `git switch` into a
branch another worktree holds fails, and the honest answer is a refusal naming the
worktree — not a forced checkout.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_localgit.py
from github_checker.localgit import (
    delete_branch,
    has_upstream,
    is_detached,
    merged_local_branches,
    switch_branch,
    worktree_holding,
)


def _init(path: Path) -> None:
    _run(path, "init", "-q", "-b", "master")
    _run(path, "config", "user.email", "t@example.com")
    _run(path, "config", "user.name", "t")
    (path / "f.txt").write_text("one\n")
    _run(path, "add", "f.txt")
    _run(path, "commit", "-qm", "init")


def _run(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )


def test_is_detached_reflects_head_state(tmp_path: Path) -> None:
    _init(tmp_path)
    assert is_detached(tmp_path) is False
    sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    _run(tmp_path, "checkout", "-q", sha)
    assert is_detached(tmp_path) is True


def test_has_upstream_is_false_without_a_remote(tmp_path: Path) -> None:
    _init(tmp_path)
    assert has_upstream(tmp_path, "master") is False


def test_worktree_holding_names_the_other_worktree(tmp_path: Path) -> None:
    main = tmp_path / "main"
    main.mkdir()
    _init(main)
    _run(main, "branch", "feature")
    other = tmp_path / "wt"
    _run(main, "worktree", "add", "-q", str(other), "feature")
    holder = worktree_holding(main, "feature")
    assert holder is not None
    assert "wt" in holder
    assert worktree_holding(main, "master") is None


def test_switch_branch_moves_head(tmp_path: Path) -> None:
    _init(tmp_path)
    _run(tmp_path, "branch", "feature")
    switch_branch(tmp_path, "feature")
    current = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert current == "feature"


def test_merged_local_branches_excludes_the_base_and_unmerged_work(
    tmp_path: Path,
) -> None:
    _init(tmp_path)
    _run(tmp_path, "checkout", "-q", "-b", "done")
    _run(tmp_path, "checkout", "-q", "master")
    _run(tmp_path, "checkout", "-q", "-b", "wip")
    (tmp_path / "g.txt").write_text("two\n")
    _run(tmp_path, "add", "g.txt")
    _run(tmp_path, "commit", "-qm", "wip")
    _run(tmp_path, "checkout", "-q", "master")
    merged = merged_local_branches(tmp_path, "master")
    assert "done" in merged
    assert "wip" not in merged
    assert "master" not in merged


def test_delete_branch_refuses_unmerged_work(tmp_path: Path) -> None:
    _init(tmp_path)
    _run(tmp_path, "checkout", "-q", "-b", "wip")
    (tmp_path / "g.txt").write_text("two\n")
    _run(tmp_path, "add", "g.txt")
    _run(tmp_path, "commit", "-qm", "wip")
    _run(tmp_path, "checkout", "-q", "master")
    with pytest.raises(LocalGitError):
        delete_branch(tmp_path, "wip")
```

Ensure `tests/test_localgit.py` imports `pytest`, `subprocess`, `Path` and
`LocalGitError` at the top if it does not already.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_localgit.py -v`
Expected: FAIL — `ImportError: cannot import name 'is_detached'`

- [ ] **Step 3: Write the implementation**

Append to `github_checker/localgit.py`:

```python
def is_detached(path: Path) -> bool:
    """True when HEAD points at a commit rather than a branch."""
    try:
        return _git(path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    except LocalGitError:
        return True


def has_upstream(path: Path, branch: str) -> bool:
    """True when *branch* has a configured upstream ref."""
    try:
        _git(path, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")
    except LocalGitError:
        return False
    return True


def worktree_holding(path: Path, branch: str) -> str | None:
    """Path of another worktree that has *branch* checked out, else None."""
    try:
        listing = _git(path, "worktree", "list", "--porcelain")
    except LocalGitError:
        return None
    current: str | None = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            current = line.removeprefix("worktree ").strip()
        elif line.startswith("branch ") and current is not None:
            ref = line.removeprefix("branch ").strip()
            if ref == f"refs/heads/{branch}" and Path(current) != path:
                return current
    return None


def switch_branch(path: Path, branch: str) -> None:
    """`git switch` to an existing branch; raises rather than forcing."""
    _git(path, "switch", branch)


def merged_local_branches(path: Path, base: str) -> list[str]:
    """Local branches fully contained in *base* (excluding *base* itself)."""
    listing = _git(path, "branch", "--merged", base, "--format=%(refname:short)")
    return [name for name in listing.splitlines() if name and name != base]


def delete_branch(path: Path, branch: str) -> None:
    """Delete a local branch with `-d` — never `-D`; unmerged work raises."""
    _git(path, "branch", "-d", branch)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_localgit.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && uv run ruff check . --fix
git add github_checker/localgit.py tests/test_localgit.py
git commit -m "feat(localgit): safe branch switching and merged-branch cleanup"
```

---

### Task 8: `post_merge_sync()` — local plumbing, fail-closed

**Files:**
- Modify: `github_checker/actions.py`
- Test: `tests/test_post_merge_sync.py`

**Interfaces:**
- Consumes: Task 7's helpers plus existing `fetch`, `pull_ff_only`, `default_branch`, `set_head_auto`, `local_status`, `is_git_repo`.
- Produces: `post_merge_sync(path: Path) -> ActionResult` with `action="post-merge-sync"` and `local_sync` set to `ok` / `failed` / `not_applicable`.

**Order of operations (do not reorder — each step protects the next):**
1. Path does not exist / not a git repo → `ok=True`, `local_sync="not_applicable"`. There is nothing local to sync; the remote merge is still true.
2. Working tree dirty (tracked **or** untracked) → refuse. Never stash, never reset.
3. HEAD detached → refuse.
4. `set_head_auto` then `default_branch`; unresolvable → refuse (never guess `master`/`main`).
5. Another worktree holds the default branch → refuse, naming it.
6. Default branch has no upstream → refuse.
7. `fetch --prune` → `switch` default → `pull --ff-only`.
8. Delete every branch in `merged_local_branches(default)` with `-d`; a branch that refuses to delete is reported, not forced.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_post_merge_sync.py
"""post-merge-sync never destroys local work; refusal beats cleverness."""

import subprocess
from pathlib import Path

from github_checker.actions import post_merge_sync


def _run(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )


def make_pair(tmp_path: Path) -> tuple[Path, Path]:
    """An origin repo plus a clone whose origin/HEAD is resolvable."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    seed.mkdir()
    _run(seed, "init", "-q", "-b", "master")
    _run(seed, "config", "user.email", "t@example.com")
    _run(seed, "config", "user.name", "t")
    (seed / "f.txt").write_text("one\n")
    _run(seed, "add", "f.txt")
    _run(seed, "commit", "-qm", "init")
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(seed), str(origin)],
        check=True, capture_output=True,
    )
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True
    )
    _run(clone, "config", "user.email", "t@example.com")
    _run(clone, "config", "user.name", "t")
    return origin, clone


def test_missing_clone_is_not_applicable_not_an_error(tmp_path: Path) -> None:
    result = post_merge_sync(tmp_path / "nope")
    assert result.ok is True
    assert result.local_sync == "not_applicable"


def test_clean_clone_syncs_and_reports_ok(tmp_path: Path) -> None:
    _, clone = make_pair(tmp_path)
    result = post_merge_sync(clone)
    assert result.ok is True
    assert result.local_sync == "ok"
    assert result.branch == "master"


def test_dirty_tree_is_refused_and_changes_survive(tmp_path: Path) -> None:
    _, clone = make_pair(tmp_path)
    (clone / "f.txt").write_text("local edit\n")
    result = post_merge_sync(clone)
    assert result.ok is False
    assert result.local_sync == "failed"
    assert "dirty" in (result.error or "").lower()
    assert (clone / "f.txt").read_text() == "local edit\n"


def test_untracked_file_also_refuses(tmp_path: Path) -> None:
    _, clone = make_pair(tmp_path)
    (clone / "scratch.txt").write_text("notes\n")
    result = post_merge_sync(clone)
    assert result.ok is False
    assert (clone / "scratch.txt").exists()


def test_detached_head_is_refused(tmp_path: Path) -> None:
    _, clone = make_pair(tmp_path)
    sha = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    _run(clone, "checkout", "-q", sha)
    result = post_merge_sync(clone)
    assert result.ok is False
    assert "detached" in (result.error or "").lower()


def test_default_branch_held_by_another_worktree_is_refused(tmp_path: Path) -> None:
    _, clone = make_pair(tmp_path)
    _run(clone, "checkout", "-q", "-b", "side")
    other = tmp_path / "wt"
    _run(clone, "worktree", "add", "-q", str(other), "master")
    result = post_merge_sync(clone)
    assert result.ok is False
    assert "worktree" in (result.error or "").lower()


def test_merged_branch_is_deleted_and_unmerged_work_is_kept(tmp_path: Path) -> None:
    _, clone = make_pair(tmp_path)
    _run(clone, "branch", "already-merged")
    _run(clone, "checkout", "-q", "-b", "keep-me")
    (clone / "g.txt").write_text("work\n")
    _run(clone, "add", "g.txt")
    _run(clone, "commit", "-qm", "wip")
    _run(clone, "checkout", "-q", "master")
    result = post_merge_sync(clone)
    assert result.ok is True
    branches = subprocess.run(
        ["git", "-C", str(clone), "branch", "--format=%(refname:short)"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    assert "already-merged" not in branches
    assert "keep-me" in branches
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_post_merge_sync.py -v`
Expected: FAIL — `ImportError: cannot import name 'post_merge_sync'`

- [ ] **Step 3: Write the implementation**

Append to `github_checker/actions.py`:

```python
def _sync_failure(path: Path, error: str) -> ActionResult:
    return ActionResult(
        action="post-merge-sync",
        dir=str(path),
        ok=False,
        local_sync="failed",
        error=error,
        local=local_status(path),
    )


def post_merge_sync(path: Path) -> ActionResult:
    """Return a clone to a freshly pulled default branch, destroying nothing.

    Every precondition that could cost work is a refusal, not a workaround:
    this never stashes, resets, force-switches or force-deletes.
    """
    if not is_git_repo(path):
        return ActionResult(
            action="post-merge-sync",
            dir=str(path),
            ok=True,
            local_sync="not_applicable",
            detail="no local clone to sync",
        )

    status = local_status(path)
    if status.dirty:
        return _sync_failure(path, "working tree is dirty; refusing to switch")
    if is_detached(path):
        return _sync_failure(path, "HEAD is detached; refusing to switch")

    set_head_auto(path)
    default = default_branch(path)
    if default is None:
        return _sync_failure(path, "cannot resolve the remote default branch")

    holder = worktree_holding(path, default)
    if holder is not None:
        return _sync_failure(path, f"branch {default} is checked out in {holder}")
    if not has_upstream(path, default):
        return _sync_failure(path, f"branch {default} has no upstream")

    try:
        fetch(path)
        switch_branch(path, default)
        pull_ff_only(path)
    except LocalGitError as err:
        return _sync_failure(path, str(err))

    removed: list[str] = []
    kept: list[str] = []
    for branch in merged_local_branches(path, default):
        try:
            delete_branch(path, branch)
            removed.append(branch)
        except LocalGitError:
            kept.append(branch)

    detail = f"synced {default}"
    if removed:
        detail += f"; deleted {len(removed)} merged branch(es)"
    if kept:
        detail += f"; kept {', '.join(kept)}"
    return ActionResult(
        action="post-merge-sync",
        dir=str(path),
        ok=True,
        local_sync="ok",
        detail=detail,
        branch=default,
        local=local_status(path),
    )
```

Extend the `from github_checker.localgit import (...)` block with `default_branch`,
`delete_branch`, `fetch`, `has_upstream`, `is_detached`, `merged_local_branches`,
`set_head_auto`, `switch_branch`, `worktree_holding`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_post_merge_sync.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && uv run ruff check . --fix
git add github_checker/actions.py tests/test_post_merge_sync.py
git commit -m "feat(actions): post-merge-sync local plumbing, fail-closed"
```

---

### Task 9: CLI wiring for the three verbs

**Files:**
- Modify: `github_checker/main.py`
- Test: `tests/test_main.py` (append)

**Interfaces:**
- Consumes: `pr_detail`, `merge_pr` (Tasks 5-6), `post_merge_sync` (Task 8).
- Produces: three subcommands:
  - `github-checker pr-detail <dir> <pr> [--file-limit N] [--diff-lines N]`
  - `github-checker merge <dir> <pr> --if-head <sha>`
  - `github-checker post-merge-sync <dir>`

**`pr-detail` output shape:** an `ActionResult` with `action="pr-detail"`, `ok=true`
and the `PrDetail` under `pr_detail`, so a single JSON contract covers all verbs.

**Argparse rule:** `--if-head` must **not** be `required=True` — argparse would
`exit(2)` with usage text on stderr, breaking the JSON contract. Validate inside
`_run_merge` and return `ActionResult(ok=False, ...)`, exactly as `propose-pr` does
(see the comment at `github_checker/main.py:129-133`).

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_main.py
import json

import pytest

from github_checker import main as main_module


def test_merge_without_if_head_returns_json_not_a_usage_error(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        "sys.argv", ["github-checker", "merge", "/tmp/repo", "7"]
    )
    with pytest.raises(SystemExit) as exit_info:
        main_module.main()
    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "--if-head" in payload["error"]


def test_pr_detail_prints_the_detail_and_exits_zero(monkeypatch, capsys) -> None:
    from github_checker.models import PrDetail

    detail = PrDetail(
        number=7, title="t", url="u", state="OPEN", is_draft=False,
        mergeable="MERGEABLE", head_branch="feat", head_sha="a" * 40,
        base_branch="master",
    )
    monkeypatch.setattr("github_checker.prgate.pr_detail", lambda *a, **k: detail)
    monkeypatch.setattr(
        "sys.argv", ["github-checker", "pr-detail", "/tmp/repo", "7"]
    )
    main_module.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["pr_detail"]["number"] == 7


def test_pr_detail_reports_unavailable_state_as_json(monkeypatch, capsys) -> None:
    from github_checker.prgate import GateUnavailable

    def boom(*args, **kwargs):
        raise GateUnavailable("gh pr view failed")

    monkeypatch.setattr("github_checker.prgate.pr_detail", boom)
    monkeypatch.setattr(
        "sys.argv", ["github-checker", "pr-detail", "/tmp/repo", "7"]
    )
    with pytest.raises(SystemExit) as exit_info:
        main_module.main()
    assert exit_info.value.code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main.py -k "merge or pr_detail" -v`
Expected: FAIL — argparse rejects the unknown command `merge` with exit code 2

- [ ] **Step 3: Write the implementation**

Add these handlers to `github_checker/main.py`:

Add a `TYPE_CHECKING` import at the top of `main.py` so the annotation resolves
without pulling `actions` in at module load (this file imports lazily on purpose):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from github_checker.actions import ActionResult
```

```python
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
        ActionResult(
            action="pr-detail", dir=str(args.dir), ok=True, pr_detail=detail
        )
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
    _emit(prgate.merge_pr(args.dir, args.pr, if_head=args.if_head))


def _run_post_merge_sync(args: argparse.Namespace) -> None:
    """Return the clone to a freshly pulled default branch."""
    from github_checker.actions import post_merge_sync

    _emit(post_merge_sync(args.dir))
```

Register the parsers inside `main()`, after the `propose-pr` block:

```python
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
```

and extend the dispatch chain at the bottom of `main()`:

```python
    elif args.command == "pr-detail":
        _run_pr_detail(args)
    elif args.command == "merge":
        _run_merge(args)
    elif args.command == "post-merge-sync":
        _run_post_merge_sync(args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_main.py -v`
Expected: PASS

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest -q`
Expected: PASS, no regressions

- [ ] **Step 6: Commit**

```bash
uv run ruff format . && uv run ruff check . --fix
git add github_checker/main.py tests/test_main.py
git commit -m "feat(cli): pr-detail, merge and post-merge-sync subcommands"
```

---

### Task 10: Document the verbs and close the TODO entry

**Files:**
- Modify: `README.md`, `TODO.md`

- [ ] **Step 1: Document the three verbs in `README.md`**

Extend the headless-actions section (the one describing `pull`, `open-pr`,
`propose-pr`) with:

````markdown
### Merge-gate verbs

```bash
github-checker pr-detail <dir> <pr>            # read: state, checks, files, diff, threads
github-checker merge <dir> <pr> --if-head <sha>  # squash-merge behind the gate
github-checker post-merge-sync <dir>           # switch to default, ff-pull, prune
```

`pr-detail` is a **view**. `merge` is an independent enforcement point: it re-reads
the PR and re-checks every predicate — `open`, `not-draft`, `mergeable`,
`checks-green`, `approvals`, `threads-resolved`, `squash-allowed` — plus the
caller's `--if-head` guard. A stale or widened payload cannot open the gate.
Anything that cannot be positively confirmed (including GitHub's `UNKNOWN`
mergeability) refuses.

`post-merge-sync` destroys nothing: a dirty tree, a detached HEAD, a missing
upstream, an unresolvable default branch, or a default branch held by another
worktree are all refusals. Merged branches are removed with `git branch -d`,
never `-D`. When there is no local clone the result is
`ok=true, local_sync="not_applicable"` — the remote merge is still true.

Large PRs are truncated explicitly, never silently: `files_truncated`,
`diff_truncated` and `threads_truncated` say so in the payload.
````

- [ ] **Step 2: Update `TODO.md`**

Add a completed entry naming the three verbs and the PR number once it exists,
following the format already used in that file.

- [ ] **Step 3: Final verification**

```bash
uv run ruff format . && uv run ruff check .
uv run pytest -q
```

Expected: clean format/lint output and a fully green suite. **Read the actual
output before claiming success** — do not report completion on an unexamined run.

- [ ] **Step 4: Commit**

```bash
git add README.md TODO.md
git commit -m "docs: merge-gate verbs and their fail-closed guarantees"
```

---

## Handoff

This repo's half of S1 is done when all ten tasks are committed and the suite is
green. Open a PR per this repo's rules (PR-only, Copilot review actioned, **human
merges**). The dispatcher plan
(`dispatcher/docs/superpowers/plans/2026-07-30-merge-gate-console.md`) depends on
these verbs and should land second.
