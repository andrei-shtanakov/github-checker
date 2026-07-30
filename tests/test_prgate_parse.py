"""gh pr view JSON -> PrDetail, including truncation of large file lists."""

from github_checker.prgate import parse_pr_view


def _view(**overrides: object) -> dict:
    data: dict[str, object] = {
        "number": 7,
        "title": "Add widget",
        "url": "https://github.com/acme/widget/pull/7",
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefName": "feat/widget",
        "headRefOid": "a" * 40,
        "baseRefName": "master",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "name": "tests",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            }
        ],
        "files": [{"path": "a.py", "additions": 3, "deletions": 1}],
        "changedFiles": 1,
    }
    data.update(overrides)
    return data


def test_parse_maps_core_fields() -> None:
    detail = parse_pr_view(_view(), file_limit=100)
    assert detail.number == 7
    assert detail.head_sha == "a" * 40
    assert detail.base_branch == "master"
    assert detail.is_draft is False
    assert detail.mergeable == "MERGEABLE"
    assert detail.review_decision == "APPROVED"
    assert detail.review_threads == []


def test_parse_normalises_check_runs_and_status_contexts() -> None:
    detail = parse_pr_view(
        _view(
            statusCheckRollup=[
                {
                    "__typename": "CheckRun",
                    "name": "tests",
                    "status": "COMPLETED",
                    "conclusion": "FAILURE",
                },
                {"__typename": "StatusContext", "context": "ci", "state": "SUCCESS"},
                {
                    "__typename": "CheckRun",
                    "name": "slow",
                    "status": "IN_PROGRESS",
                    "conclusion": None,
                },
            ]
        ),
        file_limit=100,
    )
    assert [(c.name, c.state) for c in detail.checks] == [
        ("tests", "FAILURE"),
        ("ci", "SUCCESS"),
        ("slow", "PENDING"),
    ]


def test_parse_truncates_file_list_but_keeps_the_real_total() -> None:
    files = [{"path": f"f{i}.py", "additions": 1, "deletions": 0} for i in range(150)]
    detail = parse_pr_view(_view(files=files, changedFiles=150), file_limit=100)
    assert len(detail.files) == 100
    assert detail.files_total == 150
    assert detail.files_truncated is True


def test_parse_does_not_flag_truncation_when_everything_fits() -> None:
    detail = parse_pr_view(_view(), file_limit=100)
    assert detail.files_truncated is False
    assert detail.files_total == 1


def test_parse_tolerates_missing_optional_blocks() -> None:
    detail = parse_pr_view(
        _view(statusCheckRollup=None, files=None, reviewDecision=None), file_limit=100
    )
    assert detail.checks == []
    assert detail.files == []
    assert detail.review_decision is None
