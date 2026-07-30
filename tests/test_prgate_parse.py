"""gh pr view JSON -> PrDetail, including truncation of large file lists."""

import json
from pathlib import Path

from github_checker import prgate
from github_checker.prgate import (
    GateUnavailable,
    fetch_review_threads,
    parse_pr_view,
    parse_review_threads,
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
    threads, cursor = parse_review_threads(_page([]))
    assert threads == []
    assert cursor is None


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
    try:
        fetch_review_threads(tmp_path, "acme", "widget", 7)
    except GateUnavailable as err:
        assert "boom" in str(err)
    else:
        raise AssertionError("expected GateUnavailable, got a normal return")


def test_fetch_threads_raises_on_unparseable_output(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        prgate, "run_gh", lambda path, *a, **kw: _FakeProc(0, stdout="not json")
    )
    try:
        fetch_review_threads(tmp_path, "acme", "widget", 7)
    except GateUnavailable:
        pass
    else:
        raise AssertionError("expected GateUnavailable, got a normal return")


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
