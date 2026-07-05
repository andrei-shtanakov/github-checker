from typing import Any

import pytest

import github_checker.github as gh
from tests.fixtures import RULESET_DETAILS, RULESETS_LIST


def _recording_gh_api(
    responses: dict[str, Any], fail_first_post_with: int | None = None
) -> tuple[Any, list[tuple[str, str, dict[str, Any] | None]]]:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    post_count = {"n": 0}

    async def fake(
        path: str, method: str = "GET", body: dict[str, Any] | None = None
    ) -> Any:
        calls.append((path, method, body))
        if method == "POST" and fail_first_post_with is not None:
            post_count["n"] += 1
            if post_count["n"] == 1:
                raise gh.GhError(fail_first_post_with, "HTTP 422: name taken")
        return responses.get(path)

    return fake, calls


def test_build_ruleset_copy_strips_service_fields() -> None:
    body = gh.build_ruleset_copy(RULESET_DETAILS)
    for field in (
        "id",
        "source",
        "source_type",
        "created_at",
        "updated_at",
        "current_user_can_bypass",
        "node_id",
        "_links",
    ):
        assert field not in body
    assert body["name"] == "Default Branch Restriction"
    assert body["rules"] == RULESET_DETAILS["rules"]
    assert body["bypass_actors"] == RULESET_DETAILS["bypass_actors"]


@pytest.mark.anyio
async def test_list_and_get(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = _recording_gh_api(
        {
            "repos/o/r/rulesets?per_page=100": RULESETS_LIST,
            "repos/o/r/rulesets/14708017": RULESET_DETAILS,
        }
    )
    monkeypatch.setattr(gh, "_gh_api", fake)
    infos = await gh.list_rulesets("o/r")
    assert [i.id for i in infos] == [14708017, 12637526]
    details = await gh.get_ruleset("o/r", 14708017)
    assert details.rules[0] == "deletion"


@pytest.mark.anyio
async def test_set_enforcement_and_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, calls = _recording_gh_api({})
    monkeypatch.setattr(gh, "_gh_api", fake)
    await gh.set_ruleset_enforcement("o/r", 1, "disabled")
    await gh.delete_ruleset("o/r", 1)
    assert calls[0] == ("repos/o/r/rulesets/1", "PUT", {"enforcement": "disabled"})
    assert calls[1] == ("repos/o/r/rulesets/1", "DELETE", None)


@pytest.mark.anyio
async def test_copy_ruleset(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, calls = _recording_gh_api({"repos/o/src/rulesets/14708017": RULESET_DETAILS})
    monkeypatch.setattr(gh, "_gh_api", fake)
    await gh.copy_ruleset("o/src", 14708017, "o/dst")
    path, method, body = calls[-1]
    assert path == "repos/o/dst/rulesets"
    assert method == "POST"
    assert body is not None and "id" not in body


@pytest.mark.anyio
async def test_copy_ruleset_retries_on_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, calls = _recording_gh_api(
        {"repos/o/src/rulesets/14708017": RULESET_DETAILS},
        fail_first_post_with=422,
    )
    monkeypatch.setattr(gh, "_gh_api", fake)
    await gh.copy_ruleset("o/src", 14708017, "o/dst")
    posts = [c for c in calls if c[1] == "POST"]
    assert len(posts) == 2
    assert posts[1][2] is not None
    assert posts[1][2]["name"] == "Default Branch Restriction (copy)"


@pytest.mark.anyio
async def test_copy_ruleset_non_422_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, _ = _recording_gh_api(
        {"repos/o/src/rulesets/14708017": RULESET_DETAILS},
        fail_first_post_with=403,
    )
    monkeypatch.setattr(gh, "_gh_api", fake)
    with pytest.raises(gh.GhError):
        await gh.copy_ruleset("o/src", 14708017, "o/dst")
