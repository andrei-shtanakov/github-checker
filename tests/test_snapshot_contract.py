"""Snapshot contract v1: the JSON shape is frozen in contracts/snapshot/v1/.

Consumers (dispatcher, fleet-check) vendor the schema and key off
`schema_version`. A failure here means a breaking change: ship it as v2
alongside v1, never by editing v1 in place.
"""

import json
from pathlib import Path

import pytest

from github_checker.snapshot import WorkspaceSnapshot

CONTRACT = Path(__file__).parent.parent / "contracts" / "snapshot" / "v1"


def test_model_matches_frozen_schema() -> None:
    frozen = json.loads((CONTRACT / "snapshot.schema.json").read_text())
    assert WorkspaceSnapshot.model_json_schema() == frozen, (
        "WorkspaceSnapshot no longer matches contracts/snapshot/v1 — "
        "a breaking change must become v2 (new directory), not an edit of v1"
    )


@pytest.mark.parametrize("name", ["snapshot_full.json", "snapshot_degraded.json"])
def test_golden_fixture_roundtrips(name: str) -> None:
    raw = (CONTRACT / "fixtures" / name).read_text()
    snapshot = WorkspaceSnapshot.model_validate_json(raw)
    assert snapshot.schema_version == 1
    # byte-level shape stability: parse → dump reproduces the fixture exactly
    assert json.loads(snapshot.model_dump_json()) == json.loads(raw)


def test_degraded_fixture_is_git_only() -> None:
    raw = (CONTRACT / "fixtures" / "snapshot_degraded.json").read_text()
    snapshot = WorkspaceSnapshot.model_validate_json(raw)
    assert snapshot.gh_error is not None
    assert all(repo.github is None for repo in snapshot.repos)
