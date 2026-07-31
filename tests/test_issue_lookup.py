# tests/test_issue_lookup.py
"""issue-lookup: scoped to one repo, confirmed by exact parse."""

import json
import subprocess
from pathlib import Path

from github_checker.issues import ISSUE_LIST_LIMIT, issue_lookup


def _issue(number: int, body: str, *, state: str = "OPEN") -> dict:
    # Uppercase default: `gh issue list` really sends "OPEN"/"CLOSED" (unlike
    # `gh search issues`, which sent lowercase) — feeding lowercase here
    # would let the state-normalisation in `_ref` go unexercised.
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
    """Stand-in for run_gh; records argv, replays a scripted list result."""

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
    _patch(monkeypatch, Gh([_issue(7, _body("wanted"), state="CLOSED")]))
    result = issue_lookup(Path("/repo"), "wanted")
    assert result.ok is True
    assert result.matches is not None
    assert len(result.matches) == 1
    ref = result.matches[0]
    assert ref.number == 7
    # gh sends "CLOSED"; normalised to lowercase — this pins that, not just
    # that closed issues count (the ask already happened either way).
    assert ref.state == "closed"
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
    """A prose-only mention must be excluded even *alongside* a genuine
    match in the same result set — proving the exclusion discriminates,
    rather than merely coinciding with an all-empty result a stub
    implementation (e.g. one that never appends to `matches` at all)
    would also produce.
    """
    prose_only = _issue(9, "from: dispatcher\n\nwe should file slug: wanted later\n")
    genuine = _issue(10, _body("wanted"))
    _patch(monkeypatch, Gh([prose_only, genuine]))
    result = issue_lookup(Path("/repo"), "wanted")
    assert [m.number for m in (result.matches or [])] == [10]


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
    call = gh.calls[0]
    # `gh issue list`, not `gh search issues`: the search index paginates
    # with no exhaustive mode and lags behind a just-created issue (see
    # issue_create's read-back); `gh issue list` reads the API directly.
    assert call[:2] == ("issue", "list")
    argv = " ".join(call)
    assert "--repo acme/widget" in argv
    assert "--owner" not in argv  # owner-wide search would cite a foreign repo
    # `gh issue list --state` genuinely accepts {open|closed|all} — unlike
    # `gh search issues --state`, which rejects "all" outright (verified
    # against gh 2.83.1). A closed request still means the ask happened.
    assert "--state all" in argv
    assert "--label inbox" in argv
    assert f"--limit {ISSUE_LIST_LIMIT}" in argv


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


def test_empty_stdout_is_a_failed_result_not_a_clean_empty(monkeypatch) -> None:
    """rc=0 with truly empty stdout is not the same fact as a real `[]`.

    A previous `json.loads(proc.stdout or "[]")` laundered this exact case
    into a clean "nothing found" — this pins that it must not.
    """

    def gh(path, *args, **kwargs):
        return subprocess.CompletedProcess(list(args), 0, "", "")

    monkeypatch.setattr("github_checker.issues.run_gh", gh)
    monkeypatch.setattr(
        "github_checker.issues.repo_slug", lambda *a, **k: ("acme", "widget")
    )
    result = issue_lookup(Path("/repo"), "wanted")
    assert result.ok is False
    assert result.matches is None or result.matches == []


def test_hitting_the_list_cap_is_a_failed_result_not_a_partial_match(
    monkeypatch,
) -> None:
    """Exactly the `--limit` back means the list may have been truncated —
    that must never read as a clean or partial `matches`.
    """
    payload = [_issue(n, _body(f"slug-{n}")) for n in range(ISSUE_LIST_LIMIT)]
    _patch(monkeypatch, Gh(payload))
    result = issue_lookup(Path("/repo"), "wanted")
    assert result.ok is False
    assert result.matches is None or result.matches == []


def _assert_unmappable(monkeypatch, payload) -> None:
    """A payload we cannot map must fail closed, never read as empty."""
    _patch(monkeypatch, Gh(payload))
    result = issue_lookup(Path("/repo"), "wanted")
    assert result.ok is False
    assert result.matches is None or result.matches == []


def test_top_level_object_instead_of_list_is_a_failed_result(monkeypatch) -> None:
    _assert_unmappable(monkeypatch, {"number": 1})


def test_top_level_scalar_is_a_failed_result(monkeypatch) -> None:
    _assert_unmappable(monkeypatch, 42)


def test_list_containing_null_is_a_failed_result(monkeypatch) -> None:
    _assert_unmappable(monkeypatch, [None])


def test_candidate_missing_number_is_a_failed_result(monkeypatch) -> None:
    item = _issue(6, _body("wanted"))
    del item["number"]
    _assert_unmappable(monkeypatch, [item])


def test_candidate_missing_body_is_a_failed_result(monkeypatch) -> None:
    """The identity field must fail closed like every other one: a
    candidate we cannot read a body for cannot be judged, so it must not
    be silently treated as 'no claim'.
    """
    item = _issue(6, _body("wanted"))
    del item["body"]
    _assert_unmappable(monkeypatch, [item])


def test_label_entry_missing_name_is_a_failed_result(monkeypatch) -> None:
    item = _issue(6, _body("wanted"))
    item["labels"] = [{}]
    _assert_unmappable(monkeypatch, [item])


def test_number_of_the_wrong_type_is_a_failed_result(monkeypatch) -> None:
    item = _issue(6, _body("wanted"))
    item["number"] = "not-a-number"
    _assert_unmappable(monkeypatch, [item])
