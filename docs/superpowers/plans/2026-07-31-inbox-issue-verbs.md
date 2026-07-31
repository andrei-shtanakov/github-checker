# Inbox-issue verbs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two headless verbs — `issue-lookup` (read) and `issue-create` (mutation) — so a caller can find an existing cross-repo inbox request by its slug, or create one, without touching the GitHub API itself.

**Architecture:** Both verbs work on **inbox issues** as defined by ADR-ECO-006: a GitHub issue labelled `inbox` whose body opens with a structural block carrying `slug:` and `from:`. Lookup narrows with GitHub search (substring, unreliable) and then **confirms by exact parse** of the structural block, so `benchmark-2` never matches `benchmark-20`. `issue-create` builds the canonical body itself from validated parts — the caller supplies prose only — and re-checks for an existing match immediately before creating, exactly as `merge` re-checks its gate. Both print one JSON `ActionResult` and exit 1 on failure, like every other headless verb here.

**Tech Stack:** Python 3.11+, pydantic v2, `gh` CLI (search + issue create), pytest, uv.

**Design source:** `_cowork_output/2026-07-31-s2-task-authoring-design.md` (dev-only workspace, not readable from shipped code). Canon: ADR-ECO-006 (issue inbox, body contract D3), ADR-ECO-004a (dispatcher's authoring authority), ADR-ECO-005 PF-2B (slug grammar). Everything needed is restated here.

## Global Constraints

- Package management: **uv only**. `uv add <pkg>`, `uv run pytest`, `uv run ruff`, `uv run pyrefly`. Never `pip`, never `uv pip install`.
- Line length **88** chars — count **characters**, not bytes; this repo has Russian comments and a byte count misreports them. `uv run ruff format .` then `uv run ruff check . --fix` before every commit.
- **`uv run pyrefly check` must report 0 errors.** It is configured (`[tool.pyrefly]`) and currently clean.
- Type hints on all functions; docstrings on public functions.
- **Headless JSON contract (non-negotiable):** every verb prints exactly one JSON `ActionResult` to stdout and exits 1 when `ok=false`. Never let argparse `exit(2)` replace it — validate inside the handler and return `ActionResult(ok=False, …)`, following the documented comment on the `propose-pr` parser (`github_checker/main.py`).
- **Never raise out of a verb.** A missing `gh`, a timeout, or malformed JSON becomes a failed `ActionResult`.
- **Fail-closed:** anything that cannot be positively confirmed is not a match, and anything unparseable is reported, never guessed past.
- Existing suite must stay green: `uv run pytest`.
- Comments are sparse and explain *why*, not *what*; Russian appears where a non-obvious guard needs justifying. Match that density — no narration.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `github_checker/models.py` | **modify** — add `IssueRef`. The existing `Issue` is **not** extended: it belongs to `snapshot`, whose contract is vendored into dispatcher, and adding fields there would change a frozen contract for an unrelated consumer. |
| `github_checker/inbox.py` | **new** — everything about the inbox body contract: slug and sender grammars, structural-block parsing, canonical body construction. Pure functions, no I/O. |
| `github_checker/issues.py` | **new** — the two verbs: `issue_lookup`, `issue_create`. Shells out through `ghcli.run_gh`. |
| `github_checker/actions.py` | **modify** — `ActionResult` gains `matches`, `malformed`, `created`, `issue`. |
| `github_checker/main.py` | **modify** — two subcommands. |
| `tests/test_inbox_parse.py` | **new** — grammars, parsing, canonical body. |
| `tests/test_issue_lookup.py` | **new** — repo scoping, exact match, malformed, all four states. |
| `tests/test_issue_create.py` | **new** — validation, canonical body, self re-lookup, lost race. |
| `tests/test_main.py` | **modify** — CLI wiring and exit codes. |

**Why `inbox.py` separate from `issues.py`:** the body contract is pure text with no `gh` involved, and it carries the subtle rules (exact match, one-slug-only, injection-safe sender). Keeping it I/O-free makes those rules testable without a single fake subprocess, which is the difference between testing them thoroughly and testing them once.

---

### Task 1: `IssueRef` and the two grammars

**Files:**
- Modify: `github_checker/models.py` (append after `Issue`)
- Create: `github_checker/inbox.py`
- Test: `tests/test_inbox_parse.py`

**Interfaces:**
- Consumes: nothing.
- Produces: model `IssueRef(number: int, title: str, state: str, url: str, author: str, labels: list[str])`; `SLUG_RE`, `SENDER_RE`; `valid_slug(value: str) -> bool`; `valid_sender(value: str) -> bool`.

**The grammars, and why each rejection matters:**

- **slug** — ADR-ECO-005 PF-2B: `[a-z0-9][a-z0-9._-]{0,63}`. Validated **before** any search, so a malformed slug never reaches GitHub.
- **sender** (`--from`) — `<canonical-repo-name>[#<slug>]`, repo name being the same lowercase shape. It is interpolated into the body's structural block, so a value containing **CR or LF would append arbitrary lines there** — including a second `slug:`, i.e. identity forged through a field that looks administrative. Empty values and control characters are rejected in the same place.

Both use fully-anchored patterns with `\Z`, not `$`. Under `re.fullmatch` the two behave identically — the trailing newline is not consumed either way — so this is defence against a later edit, not a live bug: `$` matches just before a trailing newline, so anyone switching to `.match()`/`.search()` would silently start accepting `"dispatcher\n"`, the exact value the injection guard rejects.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_inbox_parse.py
"""The inbox body contract: grammars, parsing, canonical body."""

import pytest

from github_checker.inbox import valid_sender, valid_slug


@pytest.mark.parametrize(
    "value",
    ["a", "benchmark-2", "merge-gate-pr-listing", "x9", "a.b_c-d", "a" * 64],
)
def test_valid_slugs_are_accepted(value: str) -> None:
    assert valid_slug(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",                 # empty
        "-leading-dash",    # must start alnum
        ".leading-dot",
        "Upper",            # lowercase only
        "has space",
        "has/slash",
        "a" * 65,           # 1 + 64 max
        "trailing\n",       # a trailing newline must not pass
        "two\nlines",
    ],
)
def test_invalid_slugs_are_rejected(value: str) -> None:
    assert valid_slug(value) is False


@pytest.mark.parametrize("value", ["dispatcher", "github-checker", "maestro#some-item"])
def test_valid_senders_are_accepted(value: str) -> None:
    assert valid_sender(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "dispatcher\nslug: injected",   # the injection this guard exists for
        "dispatcher\r\nslug: injected",
        "dispatcher\n",
        "dispatcher\x00",
        "dispatcher#",                  # '#' present but no slug
        "dispatcher#Bad",
        "Dispatcher",
    ],
)
def test_invalid_senders_are_rejected(value: str) -> None:
    assert valid_sender(value) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_inbox_parse.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'github_checker.inbox'`

- [ ] **Step 3: Add the model**

Append to `github_checker/models.py`:

```python
class IssueRef(BaseModel):
    """One inbox issue, with every field the authoring screen renders.

    Deliberately not an extension of `Issue`: that model belongs to the
    snapshot contract, which dispatcher vendors as a pinned copy.
    """

    number: int
    title: str
    state: str
    url: str
    author: str
    labels: list[str] = []
```

- [ ] **Step 4: Write the grammars**

```python
# github_checker/inbox.py
"""The ADR-ECO-006 inbox body contract, as pure text rules.

No `gh` and no I/O here on purpose: the subtle parts of this feature are
textual — exact slug equality, one structural `slug:` line, a sender that
cannot inject body lines — and they are worth testing without a fake
subprocess in the way.
"""

import re

# ADR-ECO-005 PF-2B. `\Z` not `$`: under `fullmatch` both reject a trailing
# newline, but `$` matches just before one — so switching to `.match()` or
# `.search()` later would silently start accepting "dispatcher\n", the exact
# value the injection guard below exists to reject.
SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")
SENDER_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}(#[a-z0-9][a-z0-9._-]{0,63})?\Z")


def valid_slug(value: str) -> bool:
    """True if *value* is a canonical plan-item slug (ADR-ECO-005 PF-2B)."""
    return bool(SLUG_RE.fullmatch(value))


def valid_sender(value: str) -> bool:
    """True if *value* is a valid `from:` — repo name, optionally `#slug`.

    Rejects CR/LF and control characters: this value is written into the
    body's structural block, so a newline would append arbitrary lines
    there — a second `slug:` included.
    """
    return bool(SENDER_RE.fullmatch(value))
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_inbox_parse.py -v`
Expected: PASS (26 parametrised cases)

- [ ] **Step 6: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add github_checker/models.py github_checker/inbox.py tests/test_inbox_parse.py
git commit -m "feat(inbox): IssueRef and the slug/sender grammars"
```

---

### Task 2: Parsing the structural block, and building it

**Files:**
- Modify: `github_checker/inbox.py`
- Test: `tests/test_inbox_parse.py` (append)

**Interfaces:**
- Consumes: `valid_slug`, `valid_sender` (Task 1).
- Produces: `slug_lines(body: str) -> list[str]` — the values of every `slug:` line in the **structural block**; `canonical_body(slug: str, sender: str, prose: str) -> str`.

**What "the structural block" is, precisely:** the leading lines of the body, up to the first blank line. ADR-ECO-006 D3's own example is `slug:` / `from:` / blank / prose. Scanning only the block is what stops a line in the *prose* that happens to read `slug: something` from being treated as identity.

**Why a list and not an `Optional`:** the count is the point. Zero means this candidate does not claim the slug; one is a claim; **more than one is malformed** and must be surfaced, not resolved by taking the first. Returning `str | None` would make "two slugs" indistinguishable from "one slug", which is the exact silent-choice this design forbids.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_inbox_parse.py
from github_checker.inbox import canonical_body, slug_lines

BODY = "slug: benchmark-2\nfrom: arbiter#crossover-gate\n\nNeed a second run.\n"


def test_slug_lines_reads_the_structural_block() -> None:
    assert slug_lines(BODY) == ["benchmark-2"]


def test_slug_lines_ignores_a_slug_mentioned_in_the_prose() -> None:
    body = "slug: real-one\nfrom: dispatcher\n\nAlso mentions slug: decoy here.\n"
    assert slug_lines(body) == ["real-one"]


def test_slug_lines_reports_every_structural_slug_not_just_the_first() -> None:
    body = "slug: one\nslug: two\nfrom: dispatcher\n\nprose\n"
    assert slug_lines(body) == ["one", "two"]


def test_slug_lines_is_empty_when_the_block_has_none() -> None:
    assert slug_lines("from: dispatcher\n\nprose\n") == []


def test_slug_lines_handles_a_body_with_no_blank_line() -> None:
    assert slug_lines("slug: only\nfrom: dispatcher\n") == ["only"]


def test_slug_lines_tolerates_crlf_and_surrounding_space() -> None:
    assert slug_lines("  slug:   spaced  \r\nfrom: dispatcher\r\n\r\nprose") == [
        "spaced"
    ]


def test_canonical_body_puts_the_structural_lines_first() -> None:
    out = canonical_body("my-slug", "dispatcher", "Some prose.\nSecond line.\n")
    assert out == (
        "slug: my-slug\nfrom: dispatcher\n\nSome prose.\nSecond line.\n"
    )


def test_canonical_body_round_trips_through_the_parser() -> None:
    out = canonical_body("round-trip", "dispatcher", "prose")
    assert slug_lines(out) == ["round-trip"]


def test_canonical_body_rejects_an_invalid_slug() -> None:
    with pytest.raises(ValueError, match="slug"):
        canonical_body("Bad Slug", "dispatcher", "prose")


def test_canonical_body_rejects_a_sender_that_would_inject_a_line() -> None:
    with pytest.raises(ValueError, match="from"):
        canonical_body("ok-slug", "dispatcher\nslug: injected", "prose")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_inbox_parse.py -k "slug_lines or canonical" -v`
Expected: FAIL — `ImportError: cannot import name 'canonical_body'`

- [ ] **Step 3: Write the implementation**

Append to `github_checker/inbox.py`:

```python
_SLUG_LINE_RE = re.compile(r"\A\s*slug:\s*(\S+)\s*\Z")


def slug_lines(body: str) -> list[str]:
    """Values of every `slug:` line in the body's structural block.

    The block is the leading lines up to the first blank one (ADR-ECO-006
    D3). Scanning only it is what keeps a `slug:` written inside the prose
    from being read as identity. The count is meaningful to the caller:
    two claims are malformed, not a first-wins choice.
    """
    values: list[str] = []
    for raw in body.replace("\r\n", "\n").split("\n"):
        if not raw.strip():
            break
        match = _SLUG_LINE_RE.match(raw)
        if match:
            values.append(match.group(1))
    return values


def canonical_body(slug: str, sender: str, prose: str) -> str:
    """Build an ADR-ECO-006 D3 body: structural block, blank line, prose.

    The caller supplies prose only — the structural lines are ours to
    write, so they cannot be spoofed by what a form submitted.
    """
    if not valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    if not valid_sender(sender):
        raise ValueError(f"invalid from: {sender!r}")
    return f"slug: {slug}\nfrom: {sender}\n\n{prose}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_inbox_parse.py -v`
Expected: PASS

- [ ] **Step 5: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add github_checker/inbox.py tests/test_inbox_parse.py
git commit -m "feat(inbox): structural-block parsing and canonical body"
```

---

### Task 3: `issue_lookup` — repo-scoped search, exact confirmation

**Files:**
- Create: `github_checker/issues.py`
- Modify: `github_checker/actions.py` (`ActionResult` fields)
- Test: `tests/test_issue_lookup.py`

**Interfaces:**
- Consumes: `slug_lines`, `valid_slug` (Tasks 1-2); `run_gh`, `repo_slug` from `github_checker.ghcli`; `IssueRef`.
- Produces: `issue_lookup(path: Path, slug: str, *, binary: str = "gh") -> ActionResult` with `matches: list[IssueRef]` and `malformed: list[IssueRef]`.

**Add to `ActionResult` in `actions.py`** (after `pr_detail`), importing `IssueRef` from `github_checker.models`:

```python
    matches: list[IssueRef] | None = None
    malformed: list[IssueRef] | None = None
    created: bool | None = None
    issue: IssueRef | None = None
```

**The four states this verb distinguishes:**

| State | `matches` | `malformed` | `ok` |
|---|---|---|---|
| nothing found | `[]` | `[]` | `true` |
| exactly one | `[issue]` | `[]` | `true` |
| several (a duplicate conflict, for the caller to judge) | `[…]` | `[]` | `true` |
| a candidate cannot be parsed | — | `[…]` | `false` |

Several matches is **not** an error here — it is a fact. Refusing to create on it is the composite caller's decision, not this verb's.

**Scope and confirmation:**
- Resolve `owner/repo` from `<dir>` via `repo_slug`, and search **only that repo**. Searching the whole owner would find the same slug in a *different* repo and refuse a legitimate request by citing an unrelated one.
- `gh search issues --repo <owner>/<name> --label inbox --state all --json number,title,state,url,author,labels,body <slug>` — the free-text term narrows; it is substring-based and is **only** a filter.
- Confirm each candidate with `slug_lines`: exactly one value equal to `slug` → match; **more than one value and one of them equals `slug`** → malformed; otherwise skip. A candidate with two slugs, *neither* ours, is someone else's problem, not a malformed result of this lookup.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_issue_lookup.py
"""issue-lookup: scoped to one repo, confirmed by exact parse."""

import json
import subprocess
from pathlib import Path

from github_checker.issues import issue_lookup


def _issue(number: int, body: str, *, state: str = "open") -> dict:
    return {
        "number": number,
        "title": f"issue {number}",
        "state": state,
        "url": f"https://github.com/acme/widget/issues/{number}",
        "author": {"login": "someone"},
        "labels": [{"name": "inbox"}],
        "body": body,
    }


def _body(*slugs: str) -> str:
    lines = "".join(f"slug: {s}\n" for s in slugs)
    return lines + "from: dispatcher\n\nprose\n"


class Gh:
    """Stand-in for run_gh; records argv, replays a scripted search result."""

    def __init__(self, payload: list[dict], returncode: int = 0) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.payload, self.returncode = payload, returncode

    def __call__(self, path, *args, **kwargs):
        self.calls.append(args)
        return subprocess.CompletedProcess(
            list(args), self.returncode, json.dumps(self.payload), ""
        )


def _patch(monkeypatch, gh: Gh) -> None:
    monkeypatch.setattr("github_checker.issues.run_gh", gh)
    monkeypatch.setattr(
        "github_checker.issues.repo_slug", lambda *a, **k: ("acme", "widget")
    )


def test_no_candidates_is_an_empty_match_not_an_error(monkeypatch) -> None:
    _patch(monkeypatch, Gh([]))
    result = issue_lookup(Path("/repo"), "wanted")
    assert result.ok is True
    assert result.matches == []
    assert result.malformed == []


def test_exactly_one_match_is_returned_with_every_screen_field(
    monkeypatch,
) -> None:
    _patch(monkeypatch, Gh([_issue(7, _body("wanted"), state="closed")]))
    result = issue_lookup(Path("/repo"), "wanted")
    assert result.ok is True
    assert len(result.matches) == 1
    ref = result.matches[0]
    assert ref.number == 7
    assert ref.state == "closed"  # closed issues count — the ask already happened
    assert ref.url.endswith("/issues/7")
    assert ref.author == "someone"
    assert ref.labels == ["inbox"]


def test_a_longer_slug_is_not_a_match(monkeypatch) -> None:
    """benchmark-2 must not match benchmark-20 — the substring trap."""
    _patch(monkeypatch, Gh([_issue(8, _body("benchmark-20"))]))
    result = issue_lookup(Path("/repo"), "benchmark-2")
    assert result.matches == []
    assert result.malformed == []


def test_a_slug_only_in_the_prose_is_not_a_match(monkeypatch) -> None:
    body = "from: dispatcher\n\nwe should file slug: wanted later\n"
    _patch(monkeypatch, Gh([_issue(9, body)]))
    assert issue_lookup(Path("/repo"), "wanted").matches == []


def test_several_matches_are_all_returned_and_are_not_an_error(
    monkeypatch,
) -> None:
    _patch(
        monkeypatch,
        Gh([_issue(1, _body("wanted")), _issue(2, _body("wanted"))]),
    )
    result = issue_lookup(Path("/repo"), "wanted")
    assert result.ok is True
    assert [m.number for m in result.matches] == [1, 2]


def test_two_structural_slugs_is_malformed_not_a_match(monkeypatch) -> None:
    _patch(monkeypatch, Gh([_issue(3, _body("wanted", "other"))]))
    result = issue_lookup(Path("/repo"), "wanted")
    assert result.ok is False
    assert result.matches == []
    assert [m.number for m in result.malformed] == [3]


def test_a_foreign_double_slug_issue_is_ignored(monkeypatch) -> None:
    """Two slugs, neither ours: not our match and not our malformed."""
    _patch(monkeypatch, Gh([_issue(4, _body("alpha", "beta"))]))
    result = issue_lookup(Path("/repo"), "wanted")
    assert result.ok is True
    assert result.matches == []
    assert result.malformed == []


def test_search_is_scoped_to_the_resolved_repo(monkeypatch) -> None:
    gh = Gh([])
    _patch(monkeypatch, gh)
    issue_lookup(Path("/repo"), "wanted")
    argv = " ".join(gh.calls[0])
    assert "--repo acme/widget" in argv
    assert "--owner" not in argv       # owner-wide search would cite a foreign repo
    assert "--state all" in argv
    assert "--label inbox" in argv


def test_invalid_slug_is_refused_before_any_gh_call(monkeypatch) -> None:
    gh = Gh([])
    _patch(monkeypatch, gh)
    result = issue_lookup(Path("/repo"), "Bad Slug")
    assert result.ok is False
    assert "slug" in (result.error or "")
    assert gh.calls == []


def test_unresolvable_repo_is_a_failed_result(monkeypatch) -> None:
    monkeypatch.setattr("github_checker.issues.run_gh", Gh([]))
    monkeypatch.setattr("github_checker.issues.repo_slug", lambda *a, **k: None)
    result = issue_lookup(Path("/repo"), "wanted")
    assert result.ok is False
    assert result.matches is None or result.matches == []


def test_gh_failure_is_a_failed_result_not_an_empty_match(monkeypatch) -> None:
    """An unreadable search must never look like 'nothing found'."""
    _patch(monkeypatch, Gh([], returncode=1))
    result = issue_lookup(Path("/repo"), "wanted")
    assert result.ok is False
    assert result.matches is None or result.matches == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_issue_lookup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'github_checker.issues'`

- [ ] **Step 3: Extend `ActionResult`**

Add the four fields shown above to `ActionResult` in `github_checker/actions.py`, and add `IssueRef` to its `from github_checker.models import …` line.

- [ ] **Step 4: Write the verb**

```python
# github_checker/issues.py
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


def issue_lookup(
    path: Path, slug: str, *, binary: str = "gh"
) -> ActionResult:
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

    proc = run_gh(
        path,
        "search", "issues",
        "--repo", f"{owner}/{name}",
        "--label", "inbox",
        "--state", "all",
        "--json", SEARCH_FIELDS,
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
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_issue_lookup.py -v`
Expected: PASS (11 tests)

- [ ] **Step 6: Run the full suite, then format, lint, typecheck, commit**

```bash
uv run pytest -q
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add github_checker/issues.py github_checker/actions.py tests/test_issue_lookup.py
git commit -m "feat(issues): issue-lookup with repo scoping and exact slug match"
```

---

### Task 4: `issue_create` — validated parts, canonical body, re-check before mutating

**Files:**
- Modify: `github_checker/issues.py`
- Test: `tests/test_issue_create.py`

**Interfaces:**
- Consumes: `issue_lookup` (Task 3), `canonical_body`, `valid_slug`, `valid_sender` (Tasks 1-2).
- Produces: `issue_create(path: Path, *, slug: str, sender: str, title: str, prose: str, binary: str = "gh") -> ActionResult` with `created: bool | None` and `issue: IssueRef | None`.

**`created` is three-valued, and the three values are not interchangeable:**

| `created` | Meaning |
|---|---|
| `True` | the create call **definitively** succeeded |
| `False` | definitively not created on this attempt — the slug was already taken, or a refusal happened **before** any mutation |
| `None` | **unknown** — the transport broke during the create call, so it may or may not have landed |

A lost race is a **normal** outcome, not a failure: if the pre-create re-check finds an existing issue, return `created=False, ok=True, issue=<existing>`. The request for that slug exists, which is what the caller wanted.

**The caller supplies prose only.** `slug:` and `from:` are written by this function from validated values, so a submitted form cannot spoof them.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_issue_create.py
"""issue-create: canonical body, pre-create re-check, three-valued created."""

import json
import subprocess
from pathlib import Path

from github_checker.issues import issue_create


def _existing(slug: str) -> list[dict]:
    return [
        {
            "number": 5,
            "title": "already here",
            "state": "open",
            "url": "https://github.com/acme/widget/issues/5",
            "author": {"login": "someone"},
            "labels": [{"name": "inbox"}],
            "body": f"slug: {slug}\nfrom: dispatcher\n\nprose\n",
        }
    ]


class Gh:
    """Fake gh: scripted search payloads per call, records every argv."""

    def __init__(self, searches: list[list[dict]], create_rc: int = 0) -> None:
        self.searches, self.create_rc = searches, create_rc
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, path, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("search", "issues"):
            payload = self.searches.pop(0) if self.searches else []
            return subprocess.CompletedProcess(
                list(args), 0, json.dumps(payload), ""
            )
        if args[:2] == ("issue", "create"):
            return subprocess.CompletedProcess(
                list(args),
                self.create_rc,
                "https://github.com/acme/widget/issues/9\n",
                "" if self.create_rc == 0 else "boom",
            )
        raise AssertionError(f"unexpected gh call: {args}")


def _patch(monkeypatch, gh: Gh) -> None:
    monkeypatch.setattr("github_checker.issues.run_gh", gh)
    monkeypatch.setattr(
        "github_checker.issues.repo_slug", lambda *a, **k: ("acme", "widget")
    )


def test_creates_when_the_slug_is_free(monkeypatch) -> None:
    gh = Gh([[], _existing("wanted")])  # pre-check empty, read-back finds it
    _patch(monkeypatch, gh)
    result = issue_create(
        Path("/repo"), slug="wanted", sender="dispatcher",
        title="Please do X", prose="Because Y.\n",
    )
    assert result.ok is True
    assert result.created is True
    assert result.issue is not None
    assert result.issue.number == 5
    assert any(c[:2] == ("issue", "create") for c in gh.calls)


def test_the_body_is_canonical_and_built_here(monkeypatch) -> None:
    gh = Gh([[], _existing("wanted")])
    _patch(monkeypatch, gh)
    issue_create(
        Path("/repo"), slug="wanted", sender="dispatcher",
        title="t", prose="line one\nline two\n",
    )
    create = next(c for c in gh.calls if c[:2] == ("issue", "create"))
    body = create[create.index("--body") + 1]
    assert body == "slug: wanted\nfrom: dispatcher\n\nline one\nline two\n"
    assert "--label" in create
    assert create[create.index("--label") + 1] == "inbox"


def test_a_taken_slug_is_a_normal_outcome_not_a_failure(monkeypatch) -> None:
    gh = Gh([_existing("wanted")])
    _patch(monkeypatch, gh)
    result = issue_create(
        Path("/repo"), slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert result.ok is True
    assert result.created is False
    assert result.issue is not None and result.issue.number == 5
    assert not any(c[:2] == ("issue", "create") for c in gh.calls)


def test_an_invalid_sender_refuses_before_any_gh_call(monkeypatch) -> None:
    gh = Gh([[]])
    _patch(monkeypatch, gh)
    result = issue_create(
        Path("/repo"), slug="wanted", sender="dispatcher\nslug: injected",
        title="t", prose="p",
    )
    assert result.ok is False
    assert result.created is False
    assert gh.calls == []


def test_an_invalid_slug_refuses_before_any_gh_call(monkeypatch) -> None:
    gh = Gh([[]])
    _patch(monkeypatch, gh)
    result = issue_create(
        Path("/repo"), slug="Bad Slug", sender="dispatcher", title="t", prose="p"
    )
    assert result.ok is False
    assert result.created is False
    assert gh.calls == []


def test_a_failed_create_reports_created_none_not_false(monkeypatch) -> None:
    """The call broke — whether it landed is unknown, not known-negative."""
    gh = Gh([[]], create_rc=1)
    _patch(monkeypatch, gh)
    result = issue_create(
        Path("/repo"), slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert result.ok is False
    assert result.created is None
    assert result.issue is None


def test_created_true_survives_a_failed_read_back(monkeypatch) -> None:
    """create succeeded; only our ability to re-read it did not."""
    gh = Gh([[], []])  # pre-check empty, read-back finds nothing
    _patch(monkeypatch, gh)
    result = issue_create(
        Path("/repo"), slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert result.created is True
    assert result.issue is None
    assert result.ok is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_issue_create.py -v`
Expected: FAIL — `ImportError: cannot import name 'issue_create'`

- [ ] **Step 3: Write the verb**

Append to `github_checker/issues.py`, adding `canonical_body` and `valid_sender` to the `inbox` import:

```python
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
            action="issue-create", dir=str(path), ok=False,
            created=created, error=error,
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
            action="issue-create", dir=str(path), ok=True,
            created=False, issue=pre.matches[0],
            detail="an inbox issue for this slug already exists",
        )

    proc = run_gh(
        path,
        "issue", "create",
        "--label", "inbox",
        "--title", title,
        "--body", canonical_body(slug, sender, prose),
        binary=binary,
    )
    if proc.returncode != 0:
        # the call broke: whether the issue landed is unknown, not "no"
        return failed(
            proc.stderr.strip() or "gh issue create failed", created=None
        )

    back = issue_lookup(path, slug, binary=binary)
    created_issue = back.matches[0] if back.ok and back.matches else None
    return ActionResult(
        action="issue-create", dir=str(path), ok=True,
        created=True, issue=created_issue,
        detail=(
            "created" if created_issue is not None
            else "created, but reading it back failed"
        ),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_issue_create.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full suite, then format, lint, typecheck, commit**

```bash
uv run pytest -q
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add github_checker/issues.py tests/test_issue_create.py
git commit -m "feat(issues): issue-create with canonical body and pre-create re-check"
```

---

### Task 5: CLI wiring

**Files:**
- Modify: `github_checker/main.py`
- Test: `tests/test_main.py` (append)

**Interfaces:**
- Consumes: `issue_lookup`, `issue_create`.
- Produces: `github-checker issue-lookup <dir> --slug <slug>` and `github-checker issue-create <dir> --slug <slug> --from <sender> --title <t> --body-file <path>`.

**Argparse rule (the trap this repo already documents):** none of `--slug`, `--from`, `--title`, `--body-file` may be `required=True`. Argparse would `exit(2)` with usage on **stderr**, breaking the one-JSON-on-stdout contract. Validate inside the handler and return `ActionResult(ok=False, …)`, exactly as the `propose-pr` parser's comment explains.

`--body-file` rather than a `--body` string: prose is multi-line, and argv is where newlines and quoting go to die.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_main.py
def test_issue_create_without_from_returns_json_not_a_usage_error(
    monkeypatch, capsys, tmp_path
) -> None:
    body = tmp_path / "prose.md"
    body.write_text("prose")
    monkeypatch.setattr(
        "sys.argv",
        ["github-checker", "issue-create", "/tmp/repo",
         "--slug", "wanted", "--title", "t", "--body-file", str(body)],
    )
    with pytest.raises(SystemExit) as exit_info:
        main_module.main()
    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "--from" in payload["error"]


def test_issue_create_with_a_missing_body_file_returns_json(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["github-checker", "issue-create", "/tmp/repo", "--slug", "wanted",
         "--from", "dispatcher", "--title", "t", "--body-file", "/no/such/file"],
    )
    with pytest.raises(SystemExit) as exit_info:
        main_module.main()
    assert exit_info.value.code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_issue_lookup_prints_matches(monkeypatch, capsys) -> None:
    from github_checker.actions import ActionResult
    from github_checker.models import IssueRef

    ref = IssueRef(
        number=7, title="t", state="open",
        url="https://example.invalid/7", author="a", labels=["inbox"],
    )
    monkeypatch.setattr(
        "github_checker.issues.issue_lookup",
        lambda *a, **k: ActionResult(
            action="issue-lookup", dir="/tmp/repo", ok=True,
            matches=[ref], malformed=[],
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["github-checker", "issue-lookup", "/tmp/repo", "--slug", "wanted"],
    )
    main_module.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["matches"][0]["number"] == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main.py -k issue -v`
Expected: FAIL — argparse rejects the unknown command `issue-create` with exit code 2

- [ ] **Step 3: Write the handlers**

Add to `github_checker/main.py`:

```python
def _run_issue_lookup(args: argparse.Namespace) -> None:
    """Find inbox issues claiming a slug; print the JSON result."""
    from github_checker.actions import ActionResult
    from github_checker.issues import issue_lookup

    if not args.slug:
        _emit(
            ActionResult(
                action="issue-lookup", dir=str(args.dir), ok=False,
                error="--slug is required",
            )
        )
        return
    _emit(issue_lookup(args.dir, args.slug))


def _run_issue_create(args: argparse.Namespace) -> None:
    """Create an inbox issue from validated parts plus a prose file."""
    from github_checker.actions import ActionResult
    from github_checker.issues import issue_create

    def refuse(error: str) -> None:
        _emit(
            ActionResult(
                action="issue-create", dir=str(args.dir), ok=False,
                created=False, error=error,
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
        prose = Path(args.body_file).read_text()
    except OSError as err:
        refuse(f"cannot read --body-file: {err}")
        return
    _emit(
        issue_create(
            args.dir, slug=args.slug, sender=args.sender,
            title=args.title, prose=prose,
        )
    )
```

Register the parsers inside `main()`, after the `post-merge-sync` block:

```python
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
        "--from", dest="sender", default=None,
        help="requesting repo, optionally repo#slug",
    )
    create_p.add_argument("--title", default=None, help="issue title")
    create_p.add_argument(
        "--body-file", dest="body_file", default=None,
        help="file holding the prose; the structural block is built for you",
    )
```

and extend the dispatch chain:

```python
    elif args.command == "issue-lookup":
        _run_issue_lookup(args)
    elif args.command == "issue-create":
        _run_issue_create(args)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_main.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite, then format, lint, typecheck, commit**

```bash
uv run pytest -q
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add github_checker/main.py tests/test_main.py
git commit -m "feat(cli): issue-lookup and issue-create subcommands"
```

---

### Task 6: Document the verbs and record the work

**Files:**
- Modify: `README.md`, `TODO.md`

- [ ] **Step 1: Document in `README.md`**

Extend the headless-actions section with:

````markdown
### Inbox-issue verbs

```bash
github-checker issue-lookup <dir> --slug <slug>
github-checker issue-create <dir> --slug <slug> --from <sender> \
  --title <title> --body-file <prose-file>
```

These work on **inbox issues** (ADR-ECO-006): an issue labelled `inbox` whose
body opens with a structural block of `slug:` and `from:`, then prose.

`issue-lookup` searches **this repo only** — an identical slug in a sibling
repo is a different request — in **all states**, since a closed request still
means the conversation happened. GitHub search only narrows: every candidate is
confirmed by an exact parse of the structural block, so `benchmark-2` never
matches `benchmark-20`. It returns two lists: `matches` and `malformed`. An
issue carrying more than one `slug:` line lands in `malformed` with `ok=false`
— that is an anomaly for a human, not a first-wins choice.

Several matches is **not** an error from this verb; it is a fact the caller
judges.

`issue-create` builds the canonical body itself from validated parts — you
supply prose, it writes `slug:` and `from:` — so a submitted form cannot forge
them. `--from` is validated before any network call: a value containing CR/LF
would otherwise append lines to the structural block, a second `slug:`
included. It re-checks for an existing match immediately before creating.

`created` is three-valued and the values are not interchangeable: `true` the
create definitively succeeded; `false` definitively not created on this attempt
(the slug was taken, or a refusal happened before any mutation); `null`
**unknown** — the transport broke during the call, so it may or may not have
landed. A caller must never render `null` as "not created": the safe follow-up
is to look again, never to create again.

Losing a race is a normal outcome: if the pre-create check finds an existing
issue, the result is `created=false, ok=true` with that issue attached.
````

- [ ] **Step 2: Record the work in `TODO.md`**

Add a completed entry in the format that file already uses, naming both verbs. The PR number does not exist while you are implementing — **do not invent one**; use an obviously-marked placeholder and fill it in at merge time. Do not delete or reword any existing line.

- [ ] **Step 3: Final verification**

```bash
uv run ruff format . && uv run ruff check .
uv run pytest -q
uv run pyrefly check
```

Read the actual output before claiming success. If anything fails, fix it and say so — do not report completion over an unexamined run.

- [ ] **Step 4: Commit**

```bash
git add README.md TODO.md
git commit -m "docs: inbox-issue verbs and their fail-closed guarantees"
```

---

## Handoff

This repo's half of S2 is done when all six tasks are committed and the suite is green. Open a PR per this repo's rules (PR-only, Copilot review actioned, **human merges**). The dispatcher plan depends on these verbs and lands second.
