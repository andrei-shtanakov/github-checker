"""contracts/actions/v1: the published schema, its fixtures, and the parity
between the schema and what the binary actually emits.

The fixtures are STATIC files, not regenerated here: a golden the test
rewrites records whatever the code currently does, which is the opposite of
a contract. If a fixture stops matching, either the producer changed
(deliberate, and the fixture is updated by hand in the same commit) or it
regressed — and both need a human to say which.
"""

import json
import subprocess
from pathlib import Path

import jsonschema
import pytest

from github_checker.actions import ACTION_FIELDS, ENVELOPE_FIELDS

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "actions" / "v1"
SCHEMA_PATH = CONTRACT / "actions.schema.json"
FIXTURES = sorted((CONTRACT / "fixtures").glob("*.json"))

SCHEMA = json.loads(SCHEMA_PATH.read_text())
VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def test_the_schema_is_a_valid_2020_12_schema() -> None:
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_there_are_fixtures_at_all() -> None:
    """A glob that silently matched nothing would make every fixture test
    below vacuous."""
    assert len(FIXTURES) >= 25, f"only {len(FIXTURES)} fixtures found"


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_every_fixture_validates(path: Path) -> None:
    VALIDATOR.validate(json.loads(path.read_text()))


def test_the_fixtures_cover_every_verb_and_both_error_kinds() -> None:
    kinds, actions = set(), set()
    for path in FIXTURES:
        payload = json.loads(path.read_text())
        kinds.add(payload["result_kind"])
        if payload["result_kind"] == "action":
            actions.add(payload["action"])
    assert kinds == {"action", "cli_error", "contract_error"}
    assert actions == set(ACTION_FIELDS)


def test_the_load_bearing_nulls_are_present_in_the_fixtures() -> None:
    """The three places `null` means "unknown" each have a fixture, so the
    schema is exercised against them rather than only against happy paths."""
    by_name = {p.stem: json.loads(p.read_text()) for p in FIXTURES}
    assert by_name["issue-lookup-unread"]["matches"] is None
    assert by_name["issue-create-unknown"]["created"] is None
    assert by_name["merge-unknown"]["merged"] is None
    # and their confirmed counterparts, so the contrast is pinned too
    assert by_name["issue-lookup-free"]["matches"] == []
    assert by_name["issue-create-refused"]["created"] is False
    assert by_name["merge-gate-refused"]["merged"] is False


# --- schema <-> emitter parity ----------------------------------------------


def leaf_required(verb: str) -> set[str]:
    """The key set the schema demands of one verb's leaf."""
    key = "verb_" + verb.replace("-", "_")
    return set(SCHEMA["$defs"][key]["required"])


def leaf_properties(verb: str) -> set[str]:
    key = "verb_" + verb.replace("-", "_")
    return set(SCHEMA["$defs"][key]["properties"])


@pytest.mark.parametrize("verb", sorted(ACTION_FIELDS))
def test_schema_leaf_matches_the_emitter_key_set(verb: str) -> None:
    """The schema is hand-written and the emitter derives from ACTION_FIELDS.
    Without this test they are two sources of truth free to drift — the
    schema would keep validating fixtures it generated while the binary
    shipped something else."""
    emitted = set(ENVELOPE_FIELDS) | set(ACTION_FIELDS[verb])
    assert leaf_required(verb) == emitted, f"{verb}: required diverged"
    assert leaf_properties(verb) == emitted, f"{verb}: properties diverged"


@pytest.mark.parametrize("verb", sorted(ACTION_FIELDS))
def test_every_leaf_forbids_foreign_fields(verb: str) -> None:
    key = "verb_" + verb.replace("-", "_")
    assert SCHEMA["$defs"][key]["additionalProperties"] is False


@pytest.mark.parametrize("kind", ["cli_error", "contract_error"])
def test_the_error_variants_are_closed_and_diagnostic(kind: str) -> None:
    leaf = SCHEMA["$defs"][kind]
    assert leaf["additionalProperties"] is False
    assert leaf["properties"]["result_kind"]["const"] == kind
    assert leaf["properties"]["ok"]["const"] is False
    # `action` is a free string there: it may name an unknown verb, and it
    # must not be an enum that would tie it to an action payload
    assert leaf["properties"]["action"] == {"type": "string"}


# --- the real binary, not only the fixtures ---------------------------------


def _run(*args: str) -> dict:
    proc = subprocess.run(
        ["uv", "run", "github-checker", *args], capture_output=True, text=True
    )
    return json.loads(proc.stdout)


def test_a_real_action_result_validates() -> None:
    VALIDATOR.validate(_run("pull", "/tmp"))


def test_a_real_cli_error_validates() -> None:
    """This is the path that already caught a missing `schema_version` once:
    the field has a model default, so a model-level check passes while the
    wire payload — built with exclude_unset — silently lacks it."""
    payload = _run("merge", "/tmp", "--pr", "1", "--if-head", "--limit")
    assert payload["result_kind"] == "cli_error"
    VALIDATOR.validate(payload)


def test_an_unknown_verb_is_a_cli_error_not_an_action() -> None:
    payload = _run("no-such-verb", "/tmp")
    assert payload["result_kind"] == "cli_error"
    assert payload["action"] == "no-such-verb", "diagnostic only"
    VALIDATOR.validate(payload)
