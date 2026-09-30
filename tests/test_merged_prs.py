"""merged-prs: PRs merged since a moment across the clone's owner, with who merged."""

import json
import subprocess
from pathlib import Path

from github_checker.mergedprs import SEARCH_CEILING, merged_prs

SINCE = "2026-09-29T14:00:00+04:00"


def _node(number: int, merger: str | None = "ai-prosto", repo: str = "acme/widget"):
    return {
        "number": number,
        "title": f"pr {number}",
        "url": f"https://github.com/{repo}/pull/{number}",
        "mergedAt": "2026-09-30T09:15:33Z",
        "mergedBy": None if merger is None else {"login": merger},
        "repository": {"nameWithOwner": repo},
    }


def _page(nodes: list, count: int | None = None) -> dict:
    return {
        "data": {
            "search": {
                "issueCount": len(nodes) if count is None else count,
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": nodes,
            }
        }
    }


class Gh:
    """Stand-in for run_gh: answers the one graphql call, records argv."""

    def __init__(self, stdout: object, rc: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.stdout = stdout if isinstance(stdout, str) else json.dumps(stdout)
        self.rc, self.stderr = rc, stderr

    def __call__(self, path, *args, **kwargs):
        self.calls.append(args)
        return subprocess.CompletedProcess(
            list(args), self.rc, self.stdout, self.stderr
        )


def _patch(monkeypatch, gh: Gh) -> None:
    monkeypatch.setattr("github_checker.mergedprs.run_gh", gh)
    monkeypatch.setattr(
        "github_checker.mergedprs.repo_slug", lambda *a, **k: ("acme", "widget")
    )


def test_no_merge_is_a_confirmed_empty_list(monkeypatch) -> None:
    gh = Gh([_page([])])
    _patch(monkeypatch, gh)
    result = merged_prs(Path("/repo"), SINCE)
    assert (result.ok, result.merges) == (True, [])
    [call] = gh.calls
    assert call[:4] == ("api", "graphql", "--paginate", "--slurp")
    # The window is normalised to UTC for the search qualifier.
    assert "q=is:pr is:merged user:acme merged:>=2026-09-29T10:00:00Z" in call


def test_every_page_is_read_and_the_merger_kept(monkeypatch) -> None:
    pages = [
        _page([_node(1), _node(2, "andrei-shtanakov")], count=3),
        _page([_node(3, None, "acme/gadget")], count=3),
    ]
    _patch(monkeypatch, Gh(pages))
    result = merged_prs(Path("/repo"), SINCE)
    assert result.ok is True
    assert result.merges is not None
    assert [(m.repo, m.number, m.merged_by) for m in result.merges] == [
        ("acme/widget", 1, "ai-prosto"),
        ("acme/widget", 2, "andrei-shtanakov"),
        ("acme/gadget", 3, None),
    ]
    assert result.merges[0].merged_at == "2026-09-30T09:15:33Z"


def test_fewer_nodes_than_counted_is_unread_not_shorter(monkeypatch) -> None:
    _patch(monkeypatch, Gh([_page([_node(1)], count=2)]))
    result = merged_prs(Path("/repo"), SINCE)
    assert (result.ok, result.merges) == (False, None)
    assert "counted 2" in (result.error or "")


def test_above_the_search_ceiling_is_unread(monkeypatch) -> None:
    _patch(monkeypatch, Gh([_page([_node(1)], count=SEARCH_CEILING + 1)]))
    result = merged_prs(Path("/repo"), SINCE)
    assert (result.ok, result.merges) == (False, None)
    assert "ceiling" in (result.error or "")


def test_a_failed_call_is_unread(monkeypatch) -> None:
    _patch(monkeypatch, Gh("", rc=1, stderr="HTTP 502"))
    result = merged_prs(Path("/repo"), SINCE)
    assert (result.ok, result.merges, result.error) == (False, None, "HTTP 502")


def test_non_json_is_unread(monkeypatch) -> None:
    _patch(monkeypatch, Gh("<html>"))
    result = merged_prs(Path("/repo"), SINCE)
    assert (result.ok, result.merges) == (False, None)


def test_malformed_shapes_are_unread(monkeypatch) -> None:
    """A node that is not a PR (`{}`), no pages at all, a non-integer number."""
    bad_number = _node(1)
    bad_number["number"] = "seven"
    for stdout in ([_page([{}])], [], [_page([bad_number])], [{"errors": []}]):
        _patch(monkeypatch, Gh(stdout))
        result = merged_prs(Path("/repo"), SINCE)
        assert (result.ok, result.merges) == (False, None), stdout
        assert "payload shape" in (result.error or "")


def test_a_naive_or_garbled_since_is_refused_before_gh(monkeypatch) -> None:
    for since in ("2026-09-30T00:00:00", "yesterday", ""):
        gh = Gh([_page([])])
        _patch(monkeypatch, gh)
        result = merged_prs(Path("/repo"), since)
        assert (result.ok, result.merges) == (False, None)
        assert "--since" in (result.error or "")
        assert gh.calls == []


def test_an_unresolvable_owner_is_refused(monkeypatch) -> None:
    gh = Gh([_page([])])
    monkeypatch.setattr("github_checker.mergedprs.run_gh", gh)
    monkeypatch.setattr("github_checker.mergedprs.repo_slug", lambda *a, **k: None)
    result = merged_prs(Path("/repo"), SINCE)
    assert (result.ok, result.merges) == (False, None)
    assert gh.calls == []
