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
            return subprocess.CompletedProcess(list(args), 0, json.dumps(payload), "")
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
        Path("/repo"),
        slug="wanted",
        sender="dispatcher",
        title="Please do X",
        prose="Because Y.\n",
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
        Path("/repo"),
        slug="wanted",
        sender="dispatcher",
        title="t",
        prose="line one\nline two\n",
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
        Path("/repo"),
        slug="wanted",
        sender="dispatcher\nslug: injected",
        title="t",
        prose="p",
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
