"""snapshot/v2 fetch: classification on the open planes + attribution window."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import github_checker.github as gh
import github_checker.snapshot_v2 as sv2
from github_checker.models import LocalStatus
from github_checker.snapshot_v2 import (
    PAGE_CAP,
    build_snapshot_v2,
    fetch_all_v2,
    merged_in_window,
)

NOW = datetime.fromisoformat("2026-08-25T12:00:00+00:00")
OBSERVED = NOW.isoformat()
CUTOFF = NOW - timedelta(days=30)


def iso(delta_days: int) -> str:
    return (NOW - timedelta(days=delta_days)).isoformat()


OPEN_PULLS: list[dict[str, Any]] = [
    {
        "number": 42,
        "title": "Add feature X",
        "user": {"login": "andrei-shtanakov"},
        "head": {"ref": "feature-x"},
        "body": "Does X.\r\n\r\nEpic: eco.ops\r\n",
    },
    {
        # payload without a body key: the plane answered, the body did not
        # arrive — unavailable, never missing
        "number": 43,
        "title": "Bump httpx",
        "user": {"login": "dependabot[bot]"},
        "head": {"ref": "dependabot/pip/httpx"},
    },
]

ISSUES: list[dict[str, Any]] = [
    {
        "number": 7,
        "title": "Flaky test on CI",
        "user": {"login": "andrei-shtanakov"},
        "labels": [{"name": "bug"}],
        "body": "Just prose.",
    },
    {
        "number": 8,
        "title": "Bad tag",
        "user": {"login": "andrei-shtanakov"},
        "labels": [],
        "body": "x\n\nEpic: eco\n",
    },
    {
        "number": 42,
        "title": "Add feature X",
        "user": {"login": "andrei-shtanakov"},
        "labels": [],
        "body": "",
        "pull_request": {"url": "..."},
    },
]

CLOSED_PULLS: list[dict[str, Any]] = [
    {
        "number": 41,
        "title": "Merged, no merge SHA reported",
        "user": {"login": "andrei-shtanakov"},
        "head": {"ref": "b41"},
        "merged_at": iso(2),
        "updated_at": iso(1),
        "merge_commit_sha": None,
    },
    {
        "number": 40,
        "title": "Merged in window",
        "user": {"login": "andrei-shtanakov"},
        "head": {"ref": "b40"},
        "merged_at": iso(5),
        "updated_at": iso(5),
        "merge_commit_sha": "a" * 40,
        "body": "Fix.\n\nEpic: eco.ops\nDefect: pipeline\n",
    },
    {
        "number": 39,
        "title": "Closed unmerged",
        "user": {"login": "andrei-shtanakov"},
        "head": {"ref": "b39"},
        "merged_at": None,
        "updated_at": iso(6),
        "merge_commit_sha": "b" * 40,
    },
    {
        "number": 38,
        "title": "Merged outside window",
        "user": {"login": "andrei-shtanakov"},
        "head": {"ref": "b38"},
        "merged_at": iso(45),
        "updated_at": iso(45),
        "merge_commit_sha": "c" * 40,
    },
]

RESPONSES: dict[str, Any] = {
    "repos/o/r/pulls?state=open&per_page=100": OPEN_PULLS,
    "repos/o/r/branches?per_page=100": [{"name": "master"}],
    "repos/o/r/pulls?state=closed&sort=updated&direction=desc&per_page=100": (
        CLOSED_PULLS
    ),
    "repos/o/r/pulls/42/reviews?per_page=100": [
        {"user": {"login": "copilot-pull-request-reviewer[bot]"}, "state": "APPROVED"}
    ],
    "repos/o/r/pulls/42/comments?per_page=100": [],
    "repos/o/r/pulls/43/reviews?per_page=100": [],
    "repos/o/r/pulls/40/commits?per_page=100": [
        {"sha": "1" * 40},
        {"sha": "2" * 40},
    ],
    "repos/o/r/pulls/41/commits?per_page=100": [
        {"sha": f"{i:040d}"} for i in range(PAGE_CAP)
    ],
    "repos/o/r/dependabot/alerts?state=open&per_page=100": [{"number": 1}],
    "repos/o/r/issues?state=open&per_page=100": ISSUES,
}


def _fake_gh_api(responses: dict[str, Any]) -> Any:
    async def fake(path: str) -> Any:
        if path not in responses:
            raise gh.GhError(404, "HTTP 404: Not Found")
        return responses[path]

    return fake


async def _fetch_one(monkeypatch: pytest.MonkeyPatch) -> sv2.RepoStateV2:
    monkeypatch.setattr(gh, "_gh_api", _fake_gh_api(RESPONSES))
    return (await fetch_all_v2(["o/r"], window_days=30, now=NOW))[0]


@pytest.mark.anyio
async def test_open_pulls_are_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    state = await _fetch_one(monkeypatch)
    assert state.error is None
    tagged, unavailable = state.pulls
    assert tagged.epic.classification == "tagged"
    assert tagged.epic.epic == "eco.ops"
    assert tagged.epic.subject_uri == "gh://r/pull/42"
    assert tagged.epic.carrier == "pull_request"
    assert tagged.epic.observed_at == OBSERVED
    assert tagged.copilot_review is not None
    assert unavailable.is_dependabot
    assert unavailable.epic.classification == "unavailable"
    assert [d.code for d in unavailable.epic.diagnostics] == ["EP-UNAVAILABLE"]


@pytest.mark.anyio
async def test_issues_are_classified_and_prs_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = await _fetch_one(monkeypatch)
    assert state.issues is not None
    assert [i.number for i in state.issues] == [7, 8]
    missing, invalid = state.issues
    assert missing.epic.classification == "missing"
    assert missing.epic.subject_uri == "gh://r/issues/7"
    assert missing.epic.carrier == "issue"
    assert invalid.epic.classification == "invalid"
    assert [d.code for d in invalid.epic.diagnostics] == ["EP-GRAMMAR"]
    assert invalid.epic.diagnostics[0].raw == "eco"


@pytest.mark.anyio
async def test_attribution_window(monkeypatch: pytest.MonkeyPatch) -> None:
    state = await _fetch_one(monkeypatch)
    merged = state.merged
    assert merged is not None
    assert merged.window_days == 30
    assert merged.truncated is False  # page below cap: everything seen
    assert [p.number for p in merged.prs] == [40, 41]
    pr40 = merged.prs[0]
    assert pr40.merge_commit_sha == "a" * 40
    assert pr40.commit_shas == ["1" * 40, "2" * 40]
    assert pr40.commit_shas_truncated is False
    assert pr40.epic.classification == "tagged"
    assert pr40.epic.defect == "pipeline"
    pr41 = merged.prs[1]
    assert pr41.merge_commit_sha is None
    assert pr41.commit_shas_truncated is True  # commits page hit the cap
    assert pr41.epic.classification == "unavailable"  # no body key in payload


def test_merged_in_window_truncation_flag() -> None:
    inside = {"merged_at": iso(3), "updated_at": iso(3)}
    full_page_inside = [dict(inside, number=i) for i in range(PAGE_CAP)]
    _, truncated = merged_in_window(full_page_inside, CUTOFF)
    assert truncated is True  # oldest seen still inside: older ones may hide

    tail_outside = full_page_inside[:-1] + [
        {"number": 1, "merged_at": None, "updated_at": iso(60)}
    ]
    _, truncated = merged_in_window(tail_outside, CUTOFF)
    assert truncated is False  # oldest seen is outside: the window was covered

    short_page = full_page_inside[:5]
    merged, truncated = merged_in_window(short_page, CUTOFF)
    assert truncated is False
    assert len(merged) == 5


def test_merged_in_window_missing_updated_at_fails_closed() -> None:
    page = [
        {"number": i, "merged_at": iso(3), "updated_at": iso(3)}
        for i in range(PAGE_CAP - 1)
    ] + [{"number": 999, "merged_at": None}]
    _, truncated = merged_in_window(page, CUTOFF)
    assert truncated is True


@pytest.mark.anyio
async def test_error_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gh, "_gh_api", _fake_gh_api({}))
    state = (await fetch_all_v2(["o/r"], window_days=30, now=NOW))[0]
    assert state.error is not None
    assert state.pulls == []
    assert state.issues is None
    assert state.merged is None


@pytest.mark.anyio
async def test_build_snapshot_v2_local_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "repo-a" / ".git").mkdir(parents=True)
    local = LocalStatus(branch="master", ahead=0, behind=0, dirty=False)
    monkeypatch.setattr(sv2, "local_status", lambda d: local)
    monkeypatch.setattr(sv2, "remote_url", lambda d: "git@github.com:o/repo-a.git")
    snapshot = await build_snapshot_v2(tmp_path, include_github=False)
    assert snapshot.schema_version == 2
    assert snapshot.gh_error == "skipped (--local-only)"
    assert [r.dir for r in snapshot.repos] == ["repo-a"]
    assert snapshot.repos[0].remote == "o/repo-a"
    assert snapshot.repos[0].github is None


@pytest.mark.anyio
async def test_v1_copilot_enrichment_survives_refactor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared copilot_reviews helper must keep v1 behavior byte-identical."""
    monkeypatch.setattr(gh, "_gh_api", _fake_gh_api(RESPONSES))
    state = (await gh.fetch_all(["o/r"]))[0]
    assert state.pulls[0].copilot_review is not None
    assert state.pulls[0].copilot_review.state == "APPROVED"
    assert state.pulls[1].copilot_review is None
