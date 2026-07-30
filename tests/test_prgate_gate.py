"""The gate is fail-closed: anything not positively green blocks the merge."""

import pytest

from github_checker.models import CheckRun, PrDetail, ReviewThread
from github_checker.prgate import evaluate_gate


def make_detail(**overrides: object) -> PrDetail:
    data: dict = {
        "number": 7,
        "title": "t",
        "url": "u",
        "state": "OPEN",
        "is_draft": False,
        "mergeable": "MERGEABLE",
        "head_branch": "feat",
        "head_sha": "a" * 40,
        "base_branch": "master",
        "review_decision": "APPROVED",
        "checks": [CheckRun(name="tests", state="SUCCESS")],
        "review_threads": [],
        "allows_squash": True,
    }
    data.update(overrides)
    return PrDetail(**data)


def test_green_pr_passes() -> None:
    result = evaluate_gate(make_detail())
    assert result.passed is True
    assert result.failed == []


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"state": "CLOSED"}, "open"),
        ({"is_draft": True}, "not-draft"),
        ({"mergeable": "CONFLICTING"}, "mergeable"),
        ({"mergeable": "UNKNOWN"}, "mergeable"),
        ({"checks": [CheckRun(name="t", state="FAILURE")]}, "checks-green"),
        ({"checks": [CheckRun(name="t", state="PENDING")]}, "checks-green"),
        ({"review_decision": "CHANGES_REQUESTED"}, "approvals"),
        ({"review_decision": "REVIEW_REQUIRED"}, "approvals"),
        ({"allows_squash": False}, "squash-allowed"),
        ({"allows_squash": None}, "squash-allowed"),
    ],
)
def test_each_predicate_blocks(overrides: dict, expected: str) -> None:
    result = evaluate_gate(make_detail(**overrides))
    assert result.passed is False
    assert expected in result.failed


def test_unresolved_thread_blocks_regardless_of_author() -> None:
    detail = make_detail(
        review_threads=[
            ReviewThread(
                id="t1",
                is_resolved=True,
                author="copilot-pull-request-reviewer[bot]",
            ),
            ReviewThread(id="t2", is_resolved=False, author="a-human"),
        ]
    )
    result = evaluate_gate(detail)
    assert result.passed is False
    assert "threads-resolved" in result.failed


def test_resolved_threads_do_not_block() -> None:
    detail = make_detail(
        review_threads=[ReviewThread(id="t1", is_resolved=True, author="anyone")]
    )
    assert evaluate_gate(detail).passed is True


def test_absent_review_decision_is_allowed() -> None:
    """A repo with no required reviewers reports None, not APPROVED."""
    assert evaluate_gate(make_detail(review_decision=None)).passed is True


def test_no_checks_configured_is_allowed() -> None:
    assert evaluate_gate(make_detail(checks=[])).passed is True


def test_all_failures_are_reported_not_just_the_first() -> None:
    result = evaluate_gate(
        make_detail(is_draft=True, mergeable="UNKNOWN", allows_squash=False)
    )
    assert set(result.failed) >= {"not-draft", "mergeable", "squash-allowed"}
