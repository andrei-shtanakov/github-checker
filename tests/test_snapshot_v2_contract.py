"""Snapshot contract v2: the JSON shape is frozen in contracts/snapshot/v2/.

Same regime as v1: consumers vendor the schema and key off `schema_version`;
a breaking change ships as v3 alongside v2, never as an edit in place. On
top of the v1 checks, the golden full fixture must exercise the whole
classification vocabulary — a contract whose fixtures never show
`unavailable` invites consumers to fold it into `missing`.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from github_checker.snapshot_v2 import WorkspaceSnapshotV2

CONTRACT = Path(__file__).parent.parent / "contracts" / "snapshot" / "v2"


def test_model_matches_frozen_schema() -> None:
    frozen = json.loads((CONTRACT / "snapshot.schema.json").read_text())
    assert WorkspaceSnapshotV2.model_json_schema() == frozen, (
        "WorkspaceSnapshotV2 diverged from contracts/snapshot/v2 — an additive "
        "change must consciously update snapshot.schema.json in the same PR "
        "(stays v2); a breaking change must become v3 (new directory)"
    )


@pytest.mark.parametrize("name", ["snapshot_full.json", "snapshot_degraded.json"])
def test_golden_fixture_roundtrips(name: str) -> None:
    raw = (CONTRACT / "fixtures" / name).read_text()
    snapshot = WorkspaceSnapshotV2.model_validate_json(raw)
    assert snapshot.schema_version == 2
    # structural round-trip: parse → dump reproduces the fixture's JSON value
    # (key order/whitespace not asserted)
    assert json.loads(snapshot.model_dump_json()) == json.loads(raw)


def test_degraded_fixture_is_git_only() -> None:
    raw = (CONTRACT / "fixtures" / "snapshot_degraded.json").read_text()
    snapshot = WorkspaceSnapshotV2.model_validate_json(raw)
    assert snapshot.gh_error is not None
    assert all(repo.github is None for repo in snapshot.repos)


def _classifications() -> list[Any]:
    raw = (CONTRACT / "fixtures" / "snapshot_full.json").read_text()
    snapshot = WorkspaceSnapshotV2.model_validate_json(raw)
    objects: list[Any] = []
    for repo in snapshot.repos:
        if repo.github is None:
            continue
        objects += [pull.epic for pull in repo.github.pulls]
        objects += [issue.epic for issue in repo.github.issues or []]
        if repo.github.merged is not None:
            objects += [pr.epic for pr in repo.github.merged.prs]
    return objects


def test_full_fixture_covers_the_whole_classification_vocabulary() -> None:
    states = {obj.classification for obj in _classifications()}
    assert states == {"tagged", "missing", "invalid", "unavailable"}


def test_unavailable_is_never_missing() -> None:
    unavailable = [
        obj for obj in _classifications() if obj.classification == "unavailable"
    ]
    assert unavailable, "the full fixture must show the unavailable state"
    for obj in unavailable:
        assert obj.epic is None
        codes = [diag.code for diag in obj.diagnostics]
        assert "EP-UNAVAILABLE" in codes
        assert "EP-MISSING" not in codes


def test_full_fixture_shows_both_truncation_states() -> None:
    raw = (CONTRACT / "fixtures" / "snapshot_full.json").read_text()
    snapshot = WorkspaceSnapshotV2.model_validate_json(raw)
    windows = [
        repo.github.merged
        for repo in snapshot.repos
        if repo.github is not None and repo.github.merged is not None
    ]
    assert {window.truncated for window in windows} == {True, False}
    per_pr = [pr.commit_shas_truncated for window in windows for pr in window.prs]
    assert set(per_pr) == {True, False}
