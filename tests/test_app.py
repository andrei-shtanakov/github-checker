from datetime import datetime
from pathlib import Path

import pytest
from textual.widgets import DataTable

import github_checker.app as app_module
from github_checker.app import GithubCheckerApp, details_text, repo_row
from github_checker.config import save_config
from github_checker.models import (
    Branch,
    Config,
    CopilotReview,
    PullRequest,
    RepoState,
)

STATE = RepoState(
    name="o/r",
    pulls=[
        PullRequest(
            number=42,
            title="Add feature X",
            author="me",
            head_branch="feature-x",
            is_dependabot=False,
            copilot_review=CopilotReview(state="COMMENTED", comment_count=2),
        ),
        PullRequest(
            number=43,
            title="Bump httpx",
            author="dependabot[bot]",
            head_branch="dependabot/pip/httpx",
            is_dependabot=True,
        ),
    ],
    branches=[Branch(name="master"), Branch(name="feature-x")],
    alerts=None,
    updated_at=datetime(2026, 7, 5, 12, 0, 0),
)


def test_repo_row_normal() -> None:
    assert repo_row(STATE) == (
        "o/r",
        "2",
        "1",
        "2",
        "n/a",
        "1/2",
        "12:00:00",
    )


def test_repo_row_error() -> None:
    state = RepoState(name="o/bad", error="HTTP 404: Not Found")
    assert repo_row(state) == ("o/bad", "-", "-", "-", "-", "-", "error")


def test_repo_row_caps_at_100() -> None:
    state = RepoState(
        name="o/big",
        branches=[Branch(name=f"b{i}") for i in range(100)],
        updated_at=datetime(2026, 7, 5, 12, 0, 0),
    )
    assert repo_row(state)[3] == "100+"


def test_details_text() -> None:
    text = details_text(STATE)
    assert "#42 Add feature X (me) [copilot: commented (2)]" in text
    assert "#43 Bump httpx (dependabot[bot]) [dbot]" in text
    assert "master" in text


def test_details_text_error() -> None:
    text = details_text(RepoState(name="o/bad", error="boom"))
    assert "boom" in text


async def _noop_fetch_all(repos: list[str]) -> list[RepoState]:
    return []


@pytest.mark.anyio
async def test_app_renders_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        app.apply_states([STATE])
        await pilot.pause()
        table = app.query_one(DataTable)
        assert table.row_count == 1
