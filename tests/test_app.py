from datetime import datetime
from pathlib import Path

import pytest
from textual.widgets import DataTable, Input

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
    RepoRef,
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


@pytest.mark.anyio
async def test_sync_updates_local_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    monkeypatch.setattr(app_module.localgit, "fetch", lambda path: None)
    monkeypatch.setattr(
        app_module.localgit,
        "local_status",
        lambda path: LocalStatus(
            branch="main", ahead=0, behind=0, dirty=False, error=None
        ),
    )
    config_path = tmp_path / "repos.toml"
    save_config(
        config_path,
        Config(repos=[RepoRef(name="o/r", path=tmp_path / "clone")]),
    )
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        app.apply_states([STATE.model_copy(update={"name": "o/r"})])
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert app._states["o/r"].local is not None
        assert app._states["o/r"].local.branch == "main"


@pytest.mark.anyio
async def test_sync_without_path_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    notes: list[str] = []
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        app.apply_states([STATE.model_copy(update={"name": "o/r"})])
        await pilot.pause()
        monkeypatch.setattr(app, "notify", lambda *a, **k: notes.append(a[0]))
        await pilot.press("s")
        await pilot.pause()
        assert any("локальный путь" in n for n in notes)
        assert app._states["o/r"].local is None


@pytest.mark.anyio
async def test_set_path_writes_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    monkeypatch.setattr(app_module.localgit, "is_git_repo", lambda path: True)
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    app = GithubCheckerApp(config_path)
    clone = tmp_path / "clone"
    async with app.run_test() as pilot:
        app.apply_states([STATE.model_copy(update={"name": "o/r"})])
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        await pilot.press(*str(clone))
        await pilot.press("enter")
        await pilot.pause()
    from github_checker.config import load_config

    assert load_config(config_path).repos[0].path == clone


@pytest.mark.anyio
async def test_set_path_rejects_non_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    monkeypatch.setattr(app_module.localgit, "is_git_repo", lambda path: False)
    notes: list[str] = []
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        app.apply_states([STATE.model_copy(update={"name": "o/r"})])
        await pilot.pause()
        monkeypatch.setattr(app, "notify", lambda *a, **k: notes.append(a[0]))
        await pilot.press("l")
        await pilot.pause()
        await pilot.press(*str(tmp_path / "plain"))
        await pilot.press("enter")
        await pilot.pause()
    from github_checker.config import load_config

    assert load_config(config_path).repos[0].path is None
    assert any("git" in n for n in notes)


@pytest.mark.anyio
async def test_set_path_clears_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    config_path = tmp_path / "repos.toml"
    save_config(
        config_path,
        Config(repos=[RepoRef(name="o/r", path=tmp_path / "clone")]),
    )
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        app.apply_states([STATE.model_copy(update={"name": "o/r"})])
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        app.screen.query_one("#path-input", Input).value = ""
        await pilot.pause()
        await pilot.click("#ok")
        await pilot.pause()
    from github_checker.config import load_config

    assert load_config(config_path).repos[0].path is None


@pytest.mark.anyio
async def test_set_path_normalizes_relative_to_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    monkeypatch.setattr(app_module.localgit, "is_git_repo", lambda path: True)
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        app.apply_states([STATE.model_copy(update={"name": "o/r"})])
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        await pilot.press(*"../Maestro")
        await pilot.press("enter")
        await pilot.pause()
    from github_checker.config import load_config

    stored = load_config(config_path).repos[0].path
    assert stored is not None
    assert stored.is_absolute()
    assert stored == (workdir / ".." / "Maestro").resolve()
