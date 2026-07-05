from pathlib import Path

import pytest

import github_checker.main as main_module


def test_main_exits_on_corrupt_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "repos.toml"
    config_path.write_text("repos = [", encoding="utf-8")
    monkeypatch.setattr(main_module, "gh_ready", lambda: None)
    monkeypatch.setattr("sys.argv", ["github-checker", "--config", str(config_path)])
    with pytest.raises(SystemExit) as excinfo:
        main_module.main()
    assert excinfo.value.code == 1
    assert "repos.toml" in capsys.readouterr().err


def test_main_exits_when_gh_not_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main_module, "gh_ready", lambda: "gh не авторизован")
    monkeypatch.setattr("sys.argv", ["github-checker"])
    with pytest.raises(SystemExit) as excinfo:
        main_module.main()
    assert excinfo.value.code == 1
    assert "gh не авторизован" in capsys.readouterr().err
