"""gh pr view JSON -> PrDetail, including truncation of large file lists."""

import json
from pathlib import Path

import pytest

from github_checker import prgate
from github_checker.prgate import (
    GateUnavailable,
    fetch_allows_squash,
    fetch_review_threads,
    parse_pr_view,
    parse_review_threads,
    pr_detail,
    truncate_diff,
)


def _view(**overrides: object) -> dict:
    data: dict[str, object] = {
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
    # a connection that is genuinely present with nodes: [] is "no threads",
    # not "could not read threads" — it must return normally, not raise
    threads, cursor = parse_review_threads(_page([]))
    assert threads == []
    assert cursor is None


def test_parse_threads_raises_on_graphql_errors_key() -> None:
    page = {"errors": [{"message": "Could not resolve to a PullRequest"}]}
    with pytest.raises(GateUnavailable, match="errors"):
        parse_review_threads(page)


@pytest.mark.parametrize(
    "page",
    [
        {},
        {"data": None},
        {"data": {}},
        {"data": {"repository": None}},
        {"data": {"repository": {}}},
        {"data": {"repository": {"pullRequest": None}}},
        {"data": {"repository": {"pullRequest": {}}}},
        {"data": {"repository": {"pullRequest": {"reviewThreads": None}}}},
    ],
)
def test_parse_threads_raises_when_any_link_in_the_chain_is_missing(
    page: dict,
) -> None:
    # every break point between data and reviewThreads must fail closed,
    # never fall through to "[]" via chained .get(..., {}) defaults
    with pytest.raises(GateUnavailable, match="missing"):
        parse_review_threads(page)


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_fetch_threads_raises_when_gh_fails(tmp_path: Path, monkeypatch) -> None:
    # неизвестное состояние тредов должно закрывать ворота, а не открывать их
    monkeypatch.setattr(
        prgate, "run_gh", lambda path, *a, **kw: _FakeProc(1, stderr="boom")
    )
    with pytest.raises(GateUnavailable, match="boom"):
        fetch_review_threads(tmp_path, "acme", "widget", 7)


def test_fetch_threads_raises_on_unparseable_output(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        prgate, "run_gh", lambda path, *a, **kw: _FakeProc(0, stdout="not json")
    )
    with pytest.raises(GateUnavailable, match="non-JSON"):
        fetch_review_threads(tmp_path, "acme", "widget", 7)


def test_fetch_threads_raises_when_response_has_no_data_chain(
    tmp_path: Path, monkeypatch
) -> None:
    # a body that parses as JSON but never positively presents reviewThreads
    # (e.g. GraphQL-level errors) must not flow through as an empty list —
    # the guard lives in parse_review_threads but must protect the fetcher too
    monkeypatch.setattr(
        prgate,
        "run_gh",
        lambda path, *a, **kw: _FakeProc(0, stdout=json.dumps({"errors": ["boom"]})),
    )
    with pytest.raises(GateUnavailable, match="errors"):
        fetch_review_threads(tmp_path, "acme", "widget", 7)


def test_fetch_threads_aggregates_pages_until_exhausted(
    tmp_path: Path, monkeypatch
) -> None:
    pages = [
        _page([_thread("t1", False)], has_next=True, cursor="c1"),
        _page([_thread("t2", True)], has_next=False),
    ]
    calls: list[str] = []

    def fake_gh(path: Path, *args: str, **kwargs: object) -> _FakeProc:
        calls.append(" ".join(args))
        return _FakeProc(0, stdout=json.dumps(pages.pop(0)))

    monkeypatch.setattr(prgate, "run_gh", fake_gh)
    threads, truncated = fetch_review_threads(tmp_path, "acme", "widget", 7)
    assert [t.id for t in threads] == ["t1", "t2"]
    assert truncated is False
    assert len(calls) == 2
    assert any("cursor=c1" in call for call in calls)


def test_fetch_threads_flags_truncation_when_page_cap_hit(
    tmp_path: Path, monkeypatch
) -> None:
    def fake_gh(path: Path, *args: str, **kwargs: object) -> _FakeProc:
        # always claims another page exists; the cap must still stop us
        page = json.dumps(_page([_thread("t", False)], has_next=True))
        return _FakeProc(0, stdout=page)

    monkeypatch.setattr(prgate, "run_gh", fake_gh)
    threads, truncated = fetch_review_threads(tmp_path, "acme", "widget", 7)
    assert truncated is True
    assert len(threads) == prgate.MAX_THREAD_PAGES


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


def _fake_gh_for_pr_detail(*, diff: _FakeProc, squash: _FakeProc, thread_next: bool):
    """Dispatch `run_gh` calls to canned responses by gh subcommand.

    Mirrors what `pr_detail` actually issues (pr view / api graphql /
    api repos/... / pr diff) so the wiring between the read verb and its
    helpers is exercised end to end, not just each helper in isolation.
    """

    def fake_gh(path: Path, *args: str, **kwargs: object) -> _FakeProc:
        if args[:2] == ("pr", "view"):
            return _FakeProc(0, stdout=json.dumps(_view()))
        if args[:2] == ("api", "graphql"):
            page = json.dumps(_page([_thread("t", False)], has_next=thread_next))
            return _FakeProc(0, stdout=page)
        if len(args) >= 2 and args[0] == "api" and args[1].startswith("repos/"):
            return squash
        if args[:2] == ("pr", "diff"):
            return diff
        raise AssertionError(f"unexpected gh invocation: {args}")

    return fake_gh


def test_pr_detail_wires_threads_and_truncation_flag_together(
    tmp_path: Path, monkeypatch
) -> None:
    # this is the property the whole task pins: evaluate_gate's
    # threads-complete predicate reads threads_truncated, so it must arrive
    # attached to the same fetch that produced review_threads, never stale
    fake_gh = _fake_gh_for_pr_detail(
        diff=_FakeProc(0, stdout="diff --git a/a.py b/a.py\n"),
        squash=_FakeProc(0, stdout="true\n"),
        thread_next=True,  # always claims another page; MAX_THREAD_PAGES caps it
    )
    monkeypatch.setattr(prgate, "run_gh", fake_gh)
    monkeypatch.setattr(prgate, "repo_slug", lambda path, **kw: ("acme", "widget"))

    detail = pr_detail(tmp_path, 7)

    assert detail.threads_truncated is True
    assert len(detail.review_threads) == prgate.MAX_THREAD_PAGES
    assert detail.allows_squash is True
    assert detail.diff == "diff --git a/a.py b/a.py\n"
    assert detail.diff_truncated is False


def test_pr_detail_raises_when_repo_slug_unresolvable(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(prgate, "repo_slug", lambda path, **kw: None)
    with pytest.raises(GateUnavailable, match="repo"):
        pr_detail(tmp_path, 7)


def test_pr_detail_raises_when_gh_pr_view_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(prgate, "repo_slug", lambda path, **kw: ("acme", "widget"))
    monkeypatch.setattr(
        prgate, "run_gh", lambda path, *a, **kw: _FakeProc(1, stderr="boom")
    )
    with pytest.raises(GateUnavailable, match="boom"):
        pr_detail(tmp_path, 7)


def test_pr_detail_raises_on_non_json_pr_view_output(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(prgate, "repo_slug", lambda path, **kw: ("acme", "widget"))
    monkeypatch.setattr(
        prgate, "run_gh", lambda path, *a, **kw: _FakeProc(0, stdout="not json")
    )
    with pytest.raises(GateUnavailable, match="non-JSON"):
        pr_detail(tmp_path, 7)


def test_pr_detail_diff_fetch_is_best_effort(tmp_path: Path, monkeypatch) -> None:
    # a failed `gh pr diff` degrades the display only — it must not raise
    fake_gh = _fake_gh_for_pr_detail(
        diff=_FakeProc(1, stderr="diff unavailable"),
        squash=_FakeProc(0, stdout="true\n"),
        thread_next=False,
    )
    monkeypatch.setattr(prgate, "run_gh", fake_gh)
    monkeypatch.setattr(prgate, "repo_slug", lambda path, **kw: ("acme", "widget"))

    detail = pr_detail(tmp_path, 7)

    assert detail.diff is None
    assert detail.diff_truncated is False


def test_fetch_allows_squash_true_on_true_stdout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        prgate, "run_gh", lambda path, *a, **kw: _FakeProc(0, stdout="true\n")
    )
    assert fetch_allows_squash(tmp_path, "acme", "widget") is True


def test_fetch_allows_squash_false_on_false_stdout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        prgate, "run_gh", lambda path, *a, **kw: _FakeProc(0, stdout="false\n")
    )
    assert fetch_allows_squash(tmp_path, "acme", "widget") is False


def test_fetch_allows_squash_none_on_failure_or_unparseable_output(
    tmp_path: Path, monkeypatch
) -> None:
    # "could not determine" must stay distinct from "squash is allowed" —
    # evaluate_gate's squash-allowed predicate blocks on anything but True
    monkeypatch.setattr(
        prgate, "run_gh", lambda path, *a, **kw: _FakeProc(1, stderr="boom")
    )
    assert fetch_allows_squash(tmp_path, "acme", "widget") is None

    monkeypatch.setattr(
        prgate, "run_gh", lambda path, *a, **kw: _FakeProc(0, stdout="null\n")
    )
    assert fetch_allows_squash(tmp_path, "acme", "widget") is None
