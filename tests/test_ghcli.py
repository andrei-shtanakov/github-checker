"""Shared gh invocation wrapper: never raises, resolves owner/name."""

import json
from pathlib import Path

from github_checker.ghcli import repo_slug, run_gh


def test_run_gh_missing_binary_becomes_result(tmp_path: Path) -> None:
    proc = run_gh(tmp_path, "pr", "view", binary="definitely-not-a-real-binary")
    assert proc.returncode == 127
    assert proc.stdout == ""
    assert "definitely-not-a-real-binary" in proc.stderr


def test_run_gh_never_raises_on_os_error(monkeypatch, tmp_path: Path) -> None:
    """A huge --body argv (issue_create's unbounded prose) can hit E2BIG; a
    restricted binary can hit PermissionError. Both are OSError, not just
    FileNotFoundError — the prior except tuple only caught the latter,
    so this pins the wider guarantee.
    """

    def boom(*a, **k):
        raise OSError("Argument list too long")

    monkeypatch.setattr("subprocess.run", boom)
    proc = run_gh(tmp_path, "issue", "create", "--body", "x")
    assert proc.returncode == 127
    assert "Argument list too long" in proc.stderr


def _fake_gh(tmp_path: Path, stdout: str, code: int = 0) -> str:
    script = tmp_path / "fake_gh.py"
    script.write_text(f"import sys\nsys.stdout.write({stdout!r})\nsys.exit({code})\n")
    launcher = tmp_path / "fake_gh"
    launcher.write_text(f'#!/bin/sh\nexec python3 {script} "$@"\n')
    launcher.chmod(0o755)
    return str(launcher)


def test_repo_slug_parses_owner_and_name(tmp_path: Path) -> None:
    payload = json.dumps({"owner": {"login": "acme"}, "name": "widget"})
    slug = repo_slug(tmp_path, binary=_fake_gh(tmp_path, payload))
    assert slug == ("acme", "widget")


def test_repo_slug_returns_none_when_gh_fails(tmp_path: Path) -> None:
    assert repo_slug(tmp_path, binary=_fake_gh(tmp_path, "", code=1)) is None


def test_repo_slug_returns_none_on_garbage_output(tmp_path: Path) -> None:
    assert repo_slug(tmp_path, binary=_fake_gh(tmp_path, "not json")) is None
