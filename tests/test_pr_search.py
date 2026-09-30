"""pr-search: open PRs carrying one label across the clone's owner."""

import json
import subprocess
from pathlib import Path

from github_checker.prsearch import PR_SEARCH_LIMIT, pr_search

LABEL = "human-merge-required"


def _hit(number: int, repo: str = "acme/widget") -> dict:
    return {
        "number": number,
        "title": f"pr {number}",
        "url": f"https://github.com/{repo}/pull/{number}",
        "repository": {"name": repo.split("/")[1], "nameWithOwner": repo},
    }


class Gh:
    """Stand-in for run_gh: routes by the gh sub-command, records argv."""

    def __init__(
        self,
        hits: object,
        *,
        search_rc: int = 0,
        pulls: dict[int, object] | None = None,
        events: dict[int, str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.hits, self.search_rc = hits, search_rc
        self.pulls = pulls or {}
        self.events = events or {}

    def __call__(self, path, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("search", "prs"):
            out = self.hits if isinstance(self.hits, str) else json.dumps(self.hits)
            return subprocess.CompletedProcess(list(args), self.search_rc, out, "")
        endpoint = next(a for a in args if a.startswith("repos/"))
        number = int(endpoint.split("/")[4])
        if endpoint.endswith("/events"):
            if number not in self.events:
                return subprocess.CompletedProcess(list(args), 1, "", "boom")
            return subprocess.CompletedProcess(list(args), 0, self.events[number], "")
        if number not in self.pulls:
            return subprocess.CompletedProcess(list(args), 1, "", "boom")
        return subprocess.CompletedProcess(
            list(args), 0, json.dumps(self.pulls[number]), ""
        )


def _patch(monkeypatch, gh: Gh) -> None:
    monkeypatch.setattr("github_checker.prsearch.run_gh", gh)
    monkeypatch.setattr(
        "github_checker.prsearch.repo_slug", lambda *a, **k: ("acme", "widget")
    )


def test_no_hits_is_a_confirmed_empty_list(monkeypatch) -> None:
    gh = Gh([])
    _patch(monkeypatch, gh)
    result = pr_search(Path("/repo"), LABEL)
    assert result.ok is True
    assert result.prs == []
    search = gh.calls[0]
    assert search[:2] == ("search", "prs")
    assert ("--owner", "acme") == search[
        search.index("--owner") : search.index("--owner") + 2
    ]
    assert LABEL in search and "open" in search


def test_hits_are_enriched_with_head_and_label_time(monkeypatch) -> None:
    gh = Gh(
        [_hit(7), _hit(3, "acme/gadget")],
        pulls={
            7: {"head": {"sha": "a" * 40, "ref": "feat/x"}},
            3: {"head": {"sha": "b" * 40, "ref": "approval/w-1"}},
        },
        events={
            7: "2026-09-20T10:00:00Z\n2026-09-21T08:30:00Z\n",
            3: "2026-09-22T00:00:00Z\n",
        },
    )
    _patch(monkeypatch, gh)
    result = pr_search(Path("/repo"), LABEL)
    assert result.ok is True
    assert result.prs is not None
    by_number = {p.number: p for p in result.prs}
    seven = by_number[7]
    assert seven.repo == "acme/widget"
    assert seven.head_sha == "a" * 40
    assert seven.head_ref == "feat/x"
    # the LAST labeled event is when the current wait began (re-labeling
    # after a removal starts a new wait)
    assert seven.labeled_at == "2026-09-21T08:30:00Z"
    assert by_number[3].repo == "acme/gadget"


def test_a_failed_enrichment_is_unknown_not_a_failed_search(monkeypatch) -> None:
    gh = Gh([_hit(7)])  # no pulls, no events: both enrichment calls fail
    _patch(monkeypatch, gh)
    result = pr_search(Path("/repo"), LABEL)
    assert result.ok is True
    assert result.prs is not None
    [pr] = result.prs
    assert (pr.head_sha, pr.head_ref, pr.labeled_at) == (None, None, None)


def test_no_labeled_event_leaves_the_time_unknown(monkeypatch) -> None:
    gh = Gh(
        [_hit(7)], pulls={7: {"head": {"sha": "a" * 40, "ref": "x"}}}, events={7: ""}
    )
    _patch(monkeypatch, gh)
    [pr] = pr_search(Path("/repo"), LABEL).prs or []
    assert pr.labeled_at is None


def test_the_label_is_matched_exactly_in_the_events_filter(monkeypatch) -> None:
    gh = Gh(
        [_hit(7)], pulls={7: {"head": {"sha": "a" * 40, "ref": "x"}}}, events={7: ""}
    )
    _patch(monkeypatch, gh)
    pr_search(Path("/repo"), LABEL)
    events_call = next(c for c in gh.calls if any(a.endswith("/events") for a in c))
    jq = events_call[events_call.index("--jq") + 1]
    assert json.dumps(LABEL) in jq  # quoted as a JSON string, never spliced raw


def test_search_failure_is_unread_not_empty(monkeypatch) -> None:
    _patch(monkeypatch, Gh([], search_rc=1))
    result = pr_search(Path("/repo"), LABEL)
    assert result.ok is False
    assert result.prs is None


def test_non_json_search_output_is_unread(monkeypatch) -> None:
    _patch(monkeypatch, Gh(""))
    result = pr_search(Path("/repo"), LABEL)
    assert result.ok is False
    assert result.prs is None


def test_hitting_the_cap_is_unread_not_a_partial_list(monkeypatch) -> None:
    _patch(monkeypatch, Gh([_hit(n) for n in range(1, PR_SEARCH_LIMIT + 1)]))
    result = pr_search(Path("/repo"), LABEL)
    assert result.ok is False
    assert result.prs is None
    assert str(PR_SEARCH_LIMIT) in (result.error or "")


def test_a_malformed_hit_is_unread(monkeypatch) -> None:
    _patch(monkeypatch, Gh([{"title": "no number"}]))
    result = pr_search(Path("/repo"), LABEL)
    assert result.ok is False
    assert result.prs is None


def test_a_non_integer_number_is_unread(monkeypatch) -> None:
    hit = _hit(7)
    hit["number"] = "seven"
    _patch(monkeypatch, Gh([hit]))
    result = pr_search(Path("/repo"), LABEL)
    assert result.ok is False
    assert result.prs is None


def test_unresolvable_owner_is_refused(monkeypatch) -> None:
    monkeypatch.setattr("github_checker.prsearch.run_gh", Gh([]))
    monkeypatch.setattr("github_checker.prsearch.repo_slug", lambda *a, **k: None)
    result = pr_search(Path("/repo"), LABEL)
    assert result.ok is False
    assert result.prs is None


def test_an_invalid_label_is_refused_before_any_call(monkeypatch) -> None:
    gh = Gh([])
    _patch(monkeypatch, gh)
    for bad in ("", "a\nb", "x" * 101, "a,b", " padded"):
        result = pr_search(Path("/repo"), bad)
        assert result.ok is False
        assert result.prs is None
    assert gh.calls == []
