from github_checker.github import (
    copilot_state,
    count_copilot_comments,
    format_bypass_actor,
    parse_branches,
    parse_pull,
    parse_ruleset_details,
    parse_ruleset_info,
)
from tests.fixtures import (
    BRANCHES,
    PULLS,
    REVIEW_COMMENTS,
    REVIEWS_NO_COPILOT,
    REVIEWS_WITH_COPILOT,
    RULESET_DETAILS,
    RULESETS_LIST,
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


def test_parse_ruleset_info() -> None:
    info = parse_ruleset_info(RULESETS_LIST[0])
    assert info.id == 14708017
    assert info.name == "Default Branch Restriction"
    assert info.enforcement == "active"
    assert info.target == "branch"


def test_parse_ruleset_details() -> None:
    details = parse_ruleset_details(RULESET_DETAILS)
    assert details.include == ["refs/heads/main"]
    assert details.exclude == []
    assert details.rules == [
        "deletion",
        "non_fast_forward",
        "update",
        "pull_request",
    ]
    assert details.bypass == ["admin (role), always", "app id=946600, always"]


def test_format_bypass_actor_variants() -> None:
    assert (
        format_bypass_actor(
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
        )
        == "admin (role), always"
    )
    assert (
        format_bypass_actor(
            {
                "actor_id": 2,
                "actor_type": "RepositoryRole",
                "bypass_mode": "pull_request",
            }
        )
        == "role id=2, pull_request"
    )
    assert (
        format_bypass_actor({"actor_type": "OrganizationAdmin", "actor_id": 1})
        == "org admin, always"
    )
    assert (
        format_bypass_actor(
            {"actor_type": "Team", "actor_id": 9, "bypass_mode": "always"}
        )
        == "team id=9, always"
    )
