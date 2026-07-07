from pathlib import Path

import pytest
from pydantic import ValidationError

from github_checker.models import (
    Config,
    LocalStatus,
    PullRequest,
    RepoRef,
    RepoState,
)


def test_config_coerces_string_repos() -> None:
    config = Config(repos=["owner/repo"])
    assert config.repos == [RepoRef(name="owner/repo")]
    assert config.repos[0].path is None
    assert config.refresh_seconds == 120


def test_config_accepts_repo_ref_with_path() -> None:
    config = Config(repos=[{"name": "owner/repo", "path": "/tmp/repo"}])
    assert config.repos[0].path == Path("/tmp/repo")


def test_config_rejects_bad_repo() -> None:
    with pytest.raises(ValidationError):
        Config(repos=["not-a-repo"])


def test_repo_ref_rejects_bad_name() -> None:
    with pytest.raises(ValidationError):
        RepoRef(name="garbage")


def test_repo_state_defaults() -> None:
    state = RepoState(name="o/r")
    assert state.pulls == []
    assert state.alerts is None
    assert state.error is None
    assert state.path is None
    assert state.local is None


def test_local_status_holds_desync() -> None:
    status = LocalStatus(branch="main", ahead=2, behind=1, dirty=True, error=None)
    assert status.ahead == 2
    assert status.dirty is True


def test_pull_request_optional_copilot() -> None:
    pr = PullRequest(
        number=1, title="t", author="a", head_branch="b", is_dependabot=False
    )
    assert pr.copilot_review is None
