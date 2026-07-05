"""Trimmed copies of real GitHub REST API response shapes."""

from typing import Any

PULLS: list[dict[str, Any]] = [
    {
        "number": 42,
        "title": "Add feature X",
        "user": {"login": "andrei-shtanakov"},
        "head": {"ref": "feature-x"},
    },
    {
        "number": 43,
        "title": "Bump httpx from 0.27.0 to 0.28.1",
        "user": {"login": "dependabot[bot]"},
        "head": {"ref": "dependabot/pip/httpx-0.28.1"},
    },
]

BRANCHES: list[dict[str, Any]] = [
    {"name": "master", "protected": True},
    {"name": "feature-x", "protected": False},
]

REVIEWS_WITH_COPILOT: list[dict[str, Any]] = [
    {"user": {"login": "some-human"}, "state": "APPROVED"},
    {
        "user": {"login": "copilot-pull-request-reviewer[bot]"},
        "state": "COMMENTED",
    },
]

REVIEWS_NO_COPILOT: list[dict[str, Any]] = [
    {"user": {"login": "some-human"}, "state": "APPROVED"},
]

REVIEW_COMMENTS: list[dict[str, Any]] = [
    {"user": {"login": "copilot-pull-request-reviewer[bot]"}, "body": "nit: ..."},
    {"user": {"login": "copilot-pull-request-reviewer[bot]"}, "body": "typo"},
    {"user": {"login": "some-human"}, "body": "lgtm"},
]

ALERTS: list[dict[str, Any]] = [
    {"number": 1, "state": "open"},
    {"number": 2, "state": "open"},
]

RULESETS_LIST: list[dict[str, Any]] = [
    {
        "id": 14708017,
        "name": "Default Branch Restriction",
        "target": "branch",
        "source_type": "Repository",
        "source": "andrei-shtanakov/atp-platform",
        "enforcement": "active",
        "node_id": "RRS_lACqUmVwb3NpdG9yec4r3xLbzgDgZ9E",
        "created_at": "2025-01-15T10:00:00.000+00:00",
        "updated_at": "2025-06-01T10:00:00.000+00:00",
    },
    {
        "id": 12637526,
        "name": "dei-protection",
        "target": "branch",
        "source_type": "Repository",
        "source": "andrei-shtanakov/atp-platform",
        "enforcement": "disabled",
    },
]

RULESET_DETAILS: dict[str, Any] = {
    "id": 14708017,
    "name": "Default Branch Restriction",
    "target": "branch",
    "source_type": "Repository",
    "source": "andrei-shtanakov/atp-platform",
    "enforcement": "active",
    "current_user_can_bypass": "always",
    "node_id": "RRS_lACqUmVwb3NpdG9yec4r3xLbzgDgZ9E",
    "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
    "rules": [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {"type": "update"},
        {
            "type": "pull_request",
            "parameters": {"required_approving_review_count": 0},
        },
    ],
    "bypass_actors": [
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"},
        {"actor_id": 946600, "actor_type": "Integration", "bypass_mode": "always"},
    ],
    "created_at": "2025-01-15T10:00:00.000+00:00",
    "updated_at": "2025-06-01T10:00:00.000+00:00",
    "_links": {"self": {"href": "https://api.github.com/..."}},
}
