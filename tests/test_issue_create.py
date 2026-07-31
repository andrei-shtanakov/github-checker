"""issue-create: canonical body, pre-create re-check, three-valued created."""

import json
import subprocess
from pathlib import Path

from github_checker.issues import issue_create


def _existing(slug: str, *, number: int = 5) -> list[dict]:
    return [
        {
            "number": number,
            "title": "already here",
            "state": "open",
            "url": f"https://github.com/acme/widget/issues/{number}",
            "author": {"login": "someone"},
            "labels": [{"name": "inbox"}],
            "body": f"slug: {slug}\nfrom: dispatcher\n\nprose\n",
        }
    ]


class Gh:
    """Fake gh: scripted `gh issue list` payloads per call, records argv."""

    def __init__(self, searches: list[list[dict]], create_rc: int = 0) -> None:
        self.searches, self.create_rc = searches, create_rc
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, path, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ("issue", "list"):
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
    assert result.detail == "created"
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


def test_create_call_is_scoped_to_the_resolved_repo(monkeypatch) -> None:
    gh = Gh([[], _existing("wanted")])
    _patch(monkeypatch, gh)
    issue_create(
        Path("/repo"), slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    create = next(c for c in gh.calls if c[:2] == ("issue", "create"))
    assert "--repo" in create
    assert create[create.index("--repo") + 1] == "acme/widget"


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


def test_several_existing_matches_are_surfaced_not_silently_narrowed(
    monkeypatch,
) -> None:
    """The pre-create check found more than one claimant — README says the
    caller judges a conflict, so issue_create must not silently take [0].
    """
    dupes = _existing("wanted", number=5) + _existing("wanted", number=6)
    gh = Gh([dupes])
    _patch(monkeypatch, gh)
    result = issue_create(
        Path("/repo"), slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert result.ok is False
    assert result.created is False
    assert result.matches is not None
    assert [m.number for m in result.matches] == [5, 6]
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


def test_unresolvable_repo_refuses_before_any_gh_call(monkeypatch) -> None:
    gh = Gh([[]])
    monkeypatch.setattr("github_checker.issues.run_gh", gh)
    monkeypatch.setattr("github_checker.issues.repo_slug", lambda *a, **k: None)
    result = issue_create(
        Path("/repo"), slug="wanted", sender="dispatcher", title="t", prose="p"
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


def test_created_true_but_read_back_finds_nothing_yet(monkeypatch) -> None:
    """create succeeded; the read-back succeeded too, but came back empty.

    Distinct from a read-back that outright failed — see the next test.
    """
    gh = Gh([[], []])  # pre-check empty, read-back succeeds, finds nothing
    _patch(monkeypatch, gh)
    result = issue_create(
        Path("/repo"), slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert result.created is True
    assert result.issue is None
    assert result.ok is True
    assert result.detail == "created, but reading it back found nothing yet"


class _FlakyGh:
    """`gh issue list` succeeds (empty) for the pre-check, then fails for
    the read-back — a genuine `gh` failure, not merely an empty result.
    """

    def __init__(self) -> None:
        self.pre_done = False

    def __call__(self, path, *args, **kwargs):
        if args[:2] == ("issue", "list"):
            if not self.pre_done:
                self.pre_done = True
                return subprocess.CompletedProcess(list(args), 0, "[]", "")
            return subprocess.CompletedProcess(list(args), 1, "", "boom")
        if args[:2] == ("issue", "create"):
            return subprocess.CompletedProcess(
                list(args), 0, "https://github.com/acme/widget/issues/9\n", ""
            )
        raise AssertionError(f"unexpected gh call: {args}")


def test_created_true_survives_a_failed_read_back(monkeypatch) -> None:
    """create succeeded; only our ability to re-read it did not — distinct
    from the previous test, where the read-back succeeds but finds nothing.
    """
    monkeypatch.setattr("github_checker.issues.run_gh", _FlakyGh())
    monkeypatch.setattr(
        "github_checker.issues.repo_slug", lambda *a, **k: ("acme", "widget")
    )
    result = issue_create(
        Path("/repo"), slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert result.created is True
    assert result.issue is None
    assert result.ok is True
    assert result.detail == "created, but reading it back failed"


def test_created_true_but_read_back_finds_a_malformed_claimant(monkeypatch) -> None:
    malformed = [
        {
            "number": 11,
            "title": "x",
            "state": "open",
            "url": "https://github.com/acme/widget/issues/11",
            "author": {"login": "someone"},
            "labels": [{"name": "inbox"}],
            "body": "slug: wanted\nslug: other\nfrom: dispatcher\n\nprose\n",
        }
    ]
    gh = Gh([[], malformed])  # pre-check empty, read-back finds a malformed one
    _patch(monkeypatch, gh)
    result = issue_create(
        Path("/repo"), slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert result.created is True
    assert result.issue is None
    assert result.ok is True
    assert result.detail == "created, but the read-back found a malformed claimant"
