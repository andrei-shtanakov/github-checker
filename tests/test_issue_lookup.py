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
    assert result.matches is not None
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
    assert result.matches is not None
    assert [m.number for m in result.matches] == [1, 2]


def test_two_structural_slugs_is_malformed_not_a_match(monkeypatch) -> None:
    _patch(monkeypatch, Gh([_issue(3, _body("wanted", "other"))]))
    result = issue_lookup(Path("/repo"), "wanted")
    assert result.ok is False
    assert result.matches == []
    assert result.malformed is not None
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
    assert "--owner" not in argv  # owner-wide search would cite a foreign repo
    assert "--label inbox" in argv
    # `gh search issues --state all` is rejected by the real CLI (exit 1,
    # "invalid argument \"all\" for \"--state\" flag") — verified against
    # gh 2.83.1. Omitting `--state` already returns every state, so the
    # flag must never appear.
    assert "--state" not in argv


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
