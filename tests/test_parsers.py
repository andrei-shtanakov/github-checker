from github_checker.github import (
    copilot_state,
    count_copilot_comments,
    parse_branches,
    parse_pull,
)
from tests.fixtures import (
    BRANCHES,
    PULLS,
    REVIEW_COMMENTS,
    REVIEWS_NO_COPILOT,
    REVIEWS_WITH_COPILOT,
)


def test_parse_pull_regular() -> None:
    pr = parse_pull(PULLS[0])
    assert pr.number == 42
    assert pr.author == "andrei-shtanakov"
    assert pr.head_branch == "feature-x"
    assert not pr.is_dependabot


def test_parse_pull_dependabot() -> None:
    assert parse_pull(PULLS[1]).is_dependabot


def test_parse_branches() -> None:
    branches = parse_branches(BRANCHES)
    assert [b.name for b in branches] == ["master", "feature-x"]


def test_copilot_state_found() -> None:
    assert copilot_state(REVIEWS_WITH_COPILOT) == "COMMENTED"


def test_copilot_state_absent() -> None:
    assert copilot_state(REVIEWS_NO_COPILOT) is None


def test_count_copilot_comments() -> None:
    assert count_copilot_comments(REVIEW_COMMENTS) == 2
