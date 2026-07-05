import subprocess
from typing import Any

import pytest

import github_checker.github as gh
from tests.fixtures import (
    ALERTS,
    BRANCHES,
    PULLS,
    REVIEW_COMMENTS,
    REVIEWS_WITH_COPILOT,
)

RESPONSES: dict[str, Any] = {
    "repos/o/r/pulls?state=open&per_page=100": PULLS,
    "repos/o/r/branches?per_page=100": BRANCHES,
    "repos/o/r/pulls/42/reviews?per_page=100": REVIEWS_WITH_COPILOT,
    "repos/o/r/pulls/42/comments?per_page=100": REVIEW_COMMENTS,
    "repos/o/r/pulls/43/reviews?per_page=100": [],
    "repos/o/r/dependabot/alerts?state=open&per_page=100": ALERTS,
}


def _fake_gh_api(responses: dict[str, Any], forbidden: set[str] | None = None) -> Any:
    async def fake(path: str) -> Any:
        if forbidden and path in forbidden:
            raise gh.GhError(403, "HTTP 403: Forbidden")
        if path not in responses:
            raise gh.GhError(404, "HTTP 404: Not Found")
        return responses[path]

    return fake


@pytest.mark.anyio
async def test_fetch_repo_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gh, "_gh_api", _fake_gh_api(RESPONSES))
    state = (await gh.fetch_all(["o/r"]))[0]
    assert state.error is None
    assert len(state.pulls) == 2
    assert state.pulls[1].is_dependabot
    assert state.alerts == 2
    assert state.updated_at is not None
    copilot = state.pulls[0].copilot_review
    assert copilot is not None
    assert copilot.state == "COMMENTED"
    assert copilot.comment_count == 2
    assert state.pulls[1].copilot_review is None


@pytest.mark.anyio
async def test_fetch_repo_alerts_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden = {"repos/o/r/dependabot/alerts?state=open&per_page=100"}
    monkeypatch.setattr(gh, "_gh_api", _fake_gh_api(RESPONSES, forbidden))
    state = (await gh.fetch_all(["o/r"]))[0]
    assert state.error is None
    assert state.alerts is None


@pytest.mark.anyio
async def test_fetch_repo_error_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gh, "_gh_api", _fake_gh_api(RESPONSES))
    states = await gh.fetch_all(["o/r", "o/missing"])
    assert states[0].error is None
    assert states[1].error is not None
    assert states[1].name == "o/missing"


@pytest.mark.anyio
async def test_fetch_repo_unexpected_shape_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    malformed = dict(RESPONSES)
    malformed["repos/o/r/pulls?state=open&per_page=100"] = [{"number": 1}]
    malformed["repos/o/r2/pulls?state=open&per_page=100"] = []
    malformed["repos/o/r2/branches?per_page=100"] = []
    malformed["repos/o/r2/dependabot/alerts?state=open&per_page=100"] = []
    monkeypatch.setattr(gh, "_gh_api", _fake_gh_api(malformed))
    states = await gh.fetch_all(["o/r", "o/r2"])
    assert states[0].error is not None
    assert "KeyError" in states[0].error
    assert states[1].error is None


def test_gh_ready_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_fnf(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", raise_fnf)
    message = gh.gh_ready()
    assert message is not None and "gh" in message
