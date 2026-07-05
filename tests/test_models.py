import pytest
from pydantic import ValidationError

from github_checker.models import Config, PullRequest, RepoState


def test_config_defaults() -> None:
    config = Config(repos=["owner/repo"])
    assert config.refresh_seconds == 120


def test_config_rejects_bad_repo() -> None:
    with pytest.raises(ValidationError):
        Config(repos=["not-a-repo"])


def test_repo_state_defaults() -> None:
    state = RepoState(name="o/r")
    assert state.pulls == []
    assert state.alerts is None
    assert state.error is None


def test_pull_request_optional_copilot() -> None:
    pr = PullRequest(
        number=1,
        title="t",
        author="a",
        head_branch="b",
        is_dependabot=False,
    )
    assert pr.copilot_review is None
