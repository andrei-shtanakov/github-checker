import json
import subprocess
from pathlib import Path

import pytest

import github_checker.main as main_module
from github_checker import actions


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


def _git(path: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def _make_pair(tmp_path: Path) -> Path:
    """A bare origin + a local clone, seeded with one committed file."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "project.yaml").write_text("spec_runner:\n  max_retries: 3\n")
    _git(seed, "add", "project.yaml")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)],
        check=True,
        capture_output=True,
    )
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    return clone


def _main_exit_code() -> int:
    """Run main() and return its exit code; 0 when it returns without SystemExit."""
    try:
        main_module.main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    return 0


def test_main_propose_pr_happy_path_prints_ok_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clone = _make_pair(tmp_path)
    monkeypatch.setattr(
        actions,
        "_gh",
        lambda path, *args: subprocess.CompletedProcess(
            ["gh", *args], 0, stdout="https://github.com/o/r/pull/7\n"
        ),
    )
    content = tmp_path / "new.yaml"
    content.write_text("spec_runner:\n  max_retries: 9\n")
    monkeypatch.setattr(
        "sys.argv",
        [
            "github-checker",
            "propose-pr",
            str(clone),
            "--message",
            "bump retries",
            "--edit",
            f"project.yaml={content}",
        ],
    )
    assert _main_exit_code() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "propose-pr"
    assert payload["ok"] is True
    assert payload["pr_url"] == "https://github.com/o/r/pull/7"
    assert payload["branch"]
    assert payload["base_branch"] == "main"
    assert payload["commit_sha"]
    assert payload["changed_paths"] == ["project.yaml"]


def test_main_propose_pr_without_edit_exits_one_with_json_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clone = _make_pair(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["github-checker", "propose-pr", str(clone), "--message", "no edits"],
    )
    assert _main_exit_code() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "at least one --edit" in payload["error"]
