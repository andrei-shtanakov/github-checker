from datetime import datetime
from pathlib import Path

import pytest
from textual.widgets import DataTable

import github_checker.app as app_module
from github_checker.app import (
    GithubCheckerApp,
    details_content,
    details_text,
    local_line,
    repo_row,
)
from github_checker.config import save_config
from github_checker.models import (
    Branch,
    Config,
    CopilotReview,
    LocalStatus,
    PullRequest,
    RepoState,
    RulesetInfo,
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
        "?",
        "1/2",
        "12:00:00",
    )


def test_repo_row_error() -> None:
    state = RepoState(name="o/bad", error="HTTP 404: Not Found")
    assert repo_row(state) == ("o/bad", "-", "-", "-", "-", "-", "-", "error")


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


def test_details_text_includes_link() -> None:
    assert "https://github.com/o/r" in details_text(STATE)


def test_details_content_has_link_span() -> None:
    content = details_content(STATE)
    url = "https://github.com/o/r"
    assert url in content.plain
    assert any(span.style == f"link {url}" for span in content.spans)


def test_local_line_variants() -> None:
    up = LocalStatus(branch="main", ahead=0, behind=0, dirty=False)
    assert "up to date" in local_line(up)
    none = LocalStatus(branch="main", ahead=None, behind=None, dirty=False)
    assert "no upstream" in local_line(none)
    desync = LocalStatus(branch="main", ahead=2, behind=1, dirty=True)
    assert "↑2" in local_line(desync) and "↓1" in local_line(desync)
    assert "dirty" in local_line(desync)
    err = LocalStatus(branch=None, ahead=None, behind=None, dirty=False, error="boom")
    assert "boom" in local_line(err)


def test_details_text_shows_local_block() -> None:
    state = STATE.model_copy(
        update={
            "path": Path("/tmp/o-r"),
            "local": LocalStatus(branch="main", ahead=2, behind=1, dirty=False),
        }
    )
    text = details_text(state)
    assert "Local: /tmp/o-r" in text
    assert "↑2 ↓1" in text


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


@pytest.mark.anyio
async def test_selection_survives_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r", "o/two"]))
    second = STATE.model_copy(update={"name": "o/two"})
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        app.apply_states([STATE, second])
        await pilot.pause()
        table = app.query_one(DataTable)
        table.move_cursor(row=1)
        await pilot.pause()
        assert app._selected == "o/two"
        app.apply_states([STATE, second])
        await pilot.pause()
        assert app._selected == "o/two"
        assert table.cursor_coordinate.row == 1


@pytest.mark.anyio
async def test_action_refresh_survives_invalid_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        config_path.write_text("repos = [", encoding="utf-8")
        app.action_refresh()
        await pilot.pause()
        assert [r.name for r in app._config.repos] == ["o/r"]


@pytest.mark.anyio
async def test_add_repo_writes_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        await pilot.press("a")
        await pilot.pause()
        await pilot.press(*"o/new")
        await pilot.press("enter")
        await pilot.pause()
    from github_checker.config import load_config

    assert [r.name for r in load_config(config_path).repos] == ["o/r", "o/new"]


@pytest.mark.anyio
async def test_remove_repo_writes_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        app.apply_states([STATE.model_copy(update={"name": "o/r"})])
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await pilot.click("#yes")
        await pilot.pause()
    from github_checker.config import load_config

    assert load_config(config_path).repos == []


def _ri(ruleset_id: int, enforcement: str) -> RulesetInfo:
    return RulesetInfo(
        id=ruleset_id, name=f"rs{ruleset_id}", enforcement=enforcement, target="branch"
    )


def test_rules_cell_variants() -> None:
    from github_checker.app import rules_cell

    assert rules_cell(None) == "?"
    assert rules_cell([]) == "-"
    assert rules_cell([_ri(1, "active"), _ri(2, "disabled")]) == "✓1"
    assert rules_cell([_ri(1, "disabled"), _ri(2, "evaluate")]) == "off2"


def test_repo_row_rules_column() -> None:
    state = STATE.model_copy(update={"rulesets": [_ri(1, "active")]})
    assert repo_row(state)[5] == "✓1"
