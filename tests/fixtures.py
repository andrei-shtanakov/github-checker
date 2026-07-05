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
