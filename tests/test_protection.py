from pathlib import Path

import pytest
from textual.widgets import DataTable

import github_checker.app as app_module
import github_checker.protection as protection_module
from github_checker.app import GithubCheckerApp
from github_checker.config import save_config
from github_checker.models import Config, RepoState, RulesetDetails, RulesetInfo
from github_checker.protection import ProtectionScreen, protection_details_text

DETAILS = RulesetDetails(
    id=1,
    name="Main protection",
    enforcement="active",
    target="branch",
    include=["~DEFAULT_BRANCH"],
    exclude=["refs/heads/wip"],
    rules=["deletion", "pull_request", "exotic_rule"],
    bypass=["admin (role), always"],
)

INFO = RulesetInfo(id=1, name="Main protection", enforcement="active", target="branch")


def test_protection_details_text() -> None:
    text = protection_details_text(DETAILS)
    assert "Main protection" in text
    assert "enforcement: active" in text
    assert "default" in text  # ~DEFAULT_BRANCH -> default
    assert "refs/heads/wip" in text
    assert "запрет удаления" in text
    assert "только через PR" in text
    assert "exotic_rule" in text  # неизвестный тип — как есть
    assert "admin (role), always" in text


def test_protection_details_text_empty_lists() -> None:
    details = DETAILS.model_copy(
        update={"include": [], "exclude": [], "rules": [], "bypass": []}
    )
    text = protection_details_text(details)
    assert "(не задано)" in text
    assert "(нет)" in text
    assert "(никто)" in text


async def _noop_fetch_all(repos: list[str]) -> list[RepoState]:
    return []


def _app(tmp_path: Path) -> GithubCheckerApp:
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r", "o/dst"]))
    return GithubCheckerApp(config_path)


@pytest.mark.anyio
async def test_p_opens_protection_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)

    async def fake_list(repo: str) -> list[RulesetInfo]:
        return [INFO]

    async def fake_get(repo: str, ruleset_id: int) -> RulesetDetails:
        return DETAILS

    monkeypatch.setattr(protection_module, "list_rulesets", fake_list)
    monkeypatch.setattr(protection_module, "get_ruleset", fake_get)

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        app.apply_states([RepoState(name="o/r", rulesets=[INFO])])
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, ProtectionScreen)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 1


@pytest.mark.anyio
async def test_p_blocked_when_rulesets_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        app.apply_states([RepoState(name="o/r", rulesets=None)])
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert not isinstance(app.screen, ProtectionScreen)


@pytest.mark.anyio
async def test_toggle_enforcement_calls_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    calls: list[tuple[str, int, str]] = []
    infos = [INFO]

    async def fake_list(repo: str) -> list[RulesetInfo]:
        return list(infos)

    async def fake_get(repo: str, ruleset_id: int) -> RulesetDetails:
        return DETAILS

    async def fake_set(repo: str, ruleset_id: int, enforcement: str) -> None:
        calls.append((repo, ruleset_id, enforcement))
        infos[0] = infos[0].model_copy(update={"enforcement": enforcement})

    monkeypatch.setattr(protection_module, "list_rulesets", fake_list)
    monkeypatch.setattr(protection_module, "get_ruleset", fake_get)
    monkeypatch.setattr(protection_module, "set_ruleset_enforcement", fake_set)

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        app.apply_states([RepoState(name="o/r", rulesets=[INFO])])
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert calls == [("o/r", 1, "disabled")]
