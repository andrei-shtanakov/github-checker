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


# --- every verb through the real binary -------------------------------------
#
# The gap that hid a broken `open-pr`: the wire-shape tests build results with
# `result_for`, which production did not use there, and the model-level tests
# never look at the wire. Only driving each verb for real closes it.
REAL_INVOCATIONS = [
    ("pull", ["pull", "/tmp"]),
    ("open-pr", ["open-pr", "/tmp"]),
    ("propose-pr", ["propose-pr", "/tmp", "--message", "x", "--edit", "a=b"]),
    ("pr-detail", ["pr-detail", "/tmp", "1"]),
    ("merge", ["merge", "/tmp", "1", "--if-head", "abc"]),
    ("post-merge-sync", ["post-merge-sync", "/tmp"]),
    ("issue-lookup", ["issue-lookup", "/tmp", "--slug", "wanted"]),
    (
        "issue-create",
        [
            "issue-create",
            "/tmp",
            "--slug",
            "w",
            "--from",
            "d",
            "--title",
            "t",
            "--body-file",
            "/etc/hostname",
        ],
    ),
]


@pytest.mark.parametrize(
    "verb, argv", REAL_INVOCATIONS, ids=[v for v, _ in REAL_INVOCATIONS]
)
def test_every_verb_ships_a_payload_the_schema_accepts(
    verb: str, argv: list[str]
) -> None:
    payload = _run(*argv)
    assert payload["result_kind"] == "action", (
        f"{verb} did not ship an action result: {payload.get('error')}"
    )
    assert payload["action"] == verb
    VALIDATOR.validate(payload)


def test_a_nested_payload_keeps_its_whole_shape() -> None:
    """`exclude_unset` recurses, and a nested model has no per-action shape:
    a PrDetail built without optional arguments loses 12 of its 21 keys,
    `diff_truncated` among them, which the schema requires."""
    from github_checker.actions import envelope_dump, result_for
    from github_checker.models import PrDetail

    detail = PrDetail(
        number=1,
        title="t",
        url="u",
        state="OPEN",
        is_draft=False,
        mergeable="MERGEABLE",
        head_branch="b",
        head_sha="s",
        base_branch="m",
    )
    wire = envelope_dump(result_for("pr-detail", "/repo", ok=True, pr_detail=detail))
    nested = wire["pr_detail"]
    assert isinstance(nested, dict)
    assert set(nested) == set(detail.model_dump())
    VALIDATOR.validate(wire)


# --- the nested PrDetail payload --------------------------------------------
#
# A third of the schema — pr_detail, check_run, changed_file, review_thread —
# was exercised by nothing until these fixtures existed, and that is the hole
# a 12-key loss fell through.


def _fixture(name: str) -> dict:
    return json.loads((CONTRACT / "fixtures" / f"{name}.json").read_text())


def test_a_full_pr_detail_fixture_carries_every_field() -> None:
    """The nested shape is fixed, not per-action: all 21 fields, always."""
    from github_checker.models import PrDetail

    nested = _fixture("pr-detail-full")["pr_detail"]
    # model_fields is the authoritative list; model_construct() with no
    # arguments omits the required ones and would understate it
    assert set(nested) == set(PrDetail.model_fields)
    assert len(nested) == 21


@pytest.mark.parametrize(
    "name", ["pr-detail-full", "pr-detail-truncated", "merge-refusal-with-detail"]
)
def test_the_nested_collections_are_not_empty(name: str) -> None:
    """Empty arrays would validate while never entering check_run,
    changed_file or review_thread — the sub-schemas would stay unexercised."""
    nested = _fixture(name)["pr_detail"]
    assert nested["checks"], "check_run unexercised"
    assert nested["files"], "changed_file unexercised"
    assert nested["review_threads"], "review_thread unexercised"
    assert set(nested["checks"][0]) == {"name", "state"}
    assert set(nested["files"][0]) == {"path", "additions", "deletions"}


def test_the_truncation_flags_have_a_fixture_that_sets_them() -> None:
    """A green verdict over a truncated list is not a green verdict, so the
    flags need a payload where they are actually true."""
    nested = _fixture("pr-detail-truncated")["pr_detail"]
    assert nested["checks_truncated"] is True
    assert nested["threads_truncated"] is True


def test_a_merge_refusal_embeds_the_detail_with_the_diff_stripped() -> None:
    """`merge` answers "why refused", not "what changed": `diff` is null by
    contract there, and `diff_truncated` still has to be present."""
    nested = _fixture("merge-refusal-with-detail")["pr_detail"]
    assert nested["diff"] is None
    assert "diff_truncated" in nested


def test_a_real_pr_detail_matches_the_fixture_shape(monkeypatch) -> None:
    """The binary's own nested payload, not only a hand-built one."""
    from github_checker.actions import envelope_dump, result_for
    from github_checker.models import PrDetail

    built = PrDetail(
        number=7,
        title="t",
        url="u",
        state="OPEN",
        is_draft=False,
        mergeable="MERGEABLE",
        head_branch="b",
        head_sha="s",
        base_branch="m",
    )
    wire = envelope_dump(result_for("pr-detail", "/repo", ok=True, pr_detail=built))
    nested = wire["pr_detail"]
    assert isinstance(nested, dict)
    assert set(nested) == set(_fixture("pr-detail-full")["pr_detail"])
    VALIDATOR.validate(wire)


# --- the schema must be tight, not merely permissive ------------------------
#
# Positive tests only prove the schema ACCEPTS good payloads. Loosening it —
# dropping a `required` entry — leaves every fixture valid and every positive
# test green. Only a payload that *should* be rejected can prove tightness.


def _valid_pr_detail_payload() -> dict:
    return _fixture("pr-detail-full")


# Spelled out, NOT read from the schema: a list derived from the artefact it
# checks disappears together with whatever is deleted from that artefact, so
# dropping a `required` entry would also drop the case that would catch it.
PR_DETAIL_REQUIRED = [
    "number",
    "title",
    "url",
    "state",
    "is_draft",
    "mergeable",
    "head_branch",
    "head_sha",
    "base_branch",
    "checks",
    "checks_truncated",
    "files",
    "files_total",
    "files_truncated",
    "review_threads",
    "threads_truncated",
    "diff_truncated",
]

NESTED_REQUIRED = [("pr_detail", field) for field in PR_DETAIL_REQUIRED]


def test_the_literal_required_list_still_matches_the_schema() -> None:
    """A second, independent route to the same fact.

    The parametrised cases below are generated from the literal above, so
    replacing that literal with a value read out of the schema would also
    delete the case that catches a dropped `required` entry — you cannot
    assert "this list was typed by hand" from inside the test. This
    comparison closes that: whichever side moves, it fails.
    """
    assert set(PR_DETAIL_REQUIRED) == set(SCHEMA["$defs"]["pr_detail"]["required"])
    assert len(PR_DETAIL_REQUIRED) == 17


@pytest.mark.parametrize(
    "container, field", NESTED_REQUIRED, ids=[f for _, f in NESTED_REQUIRED]
)
def test_a_nested_payload_missing_a_required_field_is_rejected(
    container: str, field: str
) -> None:
    payload = _valid_pr_detail_payload()
    del payload[container][field]
    with pytest.raises(jsonschema.ValidationError):
        VALIDATOR.validate(payload)


@pytest.mark.parametrize("field", ["name", "state"])
def test_a_check_run_missing_a_required_field_is_rejected(field: str) -> None:
    payload = _valid_pr_detail_payload()
    del payload["pr_detail"]["checks"][0][field]
    with pytest.raises(jsonschema.ValidationError):
        VALIDATOR.validate(payload)


@pytest.mark.parametrize("field", ["path", "additions", "deletions"])
def test_a_changed_file_missing_a_required_field_is_rejected(field: str) -> None:
    payload = _valid_pr_detail_payload()
    del payload["pr_detail"]["files"][0][field]
    with pytest.raises(jsonschema.ValidationError):
        VALIDATOR.validate(payload)


@pytest.mark.parametrize("field", ["id", "is_resolved", "is_outdated"])
def test_a_review_thread_missing_a_required_field_is_rejected(field: str) -> None:
    payload = _valid_pr_detail_payload()
    del payload["pr_detail"]["review_threads"][0][field]
    with pytest.raises(jsonschema.ValidationError):
        VALIDATOR.validate(payload)


@pytest.mark.parametrize("field", ["branch", "ahead", "behind", "dirty", "error"])
def test_a_local_status_missing_a_required_field_is_rejected(field: str) -> None:
    """`error` included: the producer always emits it, so a schema that left
    it optional would document a looser shape than the binary ships."""
    payload = _fixture("pull-success")
    del payload["local"][field]
    with pytest.raises(jsonschema.ValidationError):
        VALIDATOR.validate(payload)


def test_a_foreign_field_in_a_nested_payload_is_rejected() -> None:
    payload = _valid_pr_detail_payload()
    payload["pr_detail"]["merged"] = True
    with pytest.raises(jsonschema.ValidationError):
        VALIDATOR.validate(payload)


# --- the guards the last review found missing --------------------------------


def test_every_nested_payload_is_closed() -> None:
    """`additionalProperties: false` on pr_detail alone is not enough: a
    foreign key inside a check run, a changed file or a review thread would
    pass while the outer object stayed closed."""
    for name in (
        "pr_detail",
        "check_run",
        "changed_file",
        "review_thread",
        "issue_ref",
        "local_status",
    ):
        assert SCHEMA["$defs"][name]["additionalProperties"] is False, name


@pytest.mark.parametrize(
    "collection, index",
    [("checks", 0), ("files", 0), ("review_threads", 0)],
)
def test_a_foreign_key_inside_a_nested_collection_is_rejected(
    collection: str, index: int
) -> None:
    payload = _valid_pr_detail_payload()
    payload["pr_detail"][collection][index]["smuggled"] = 1
    with pytest.raises(jsonschema.ValidationError):
        VALIDATOR.validate(payload)


def test_the_real_binary_matrix_covers_every_verb() -> None:
    """The matrix asserting nothing about its own completeness is exactly how
    a broken `open-pr` shipped: it was simply absent from the list."""
    assert {verb for verb, _ in REAL_INVOCATIONS} == set(ACTION_FIELDS)


def test_a_fixture_local_status_matches_what_the_binary_emits(tmp_path) -> None:
    """A published example that drifted from real output reads as clean while
    documenting a shape the producer no longer sends.

    Driven through the REAL binary against a REAL repository: every entry in
    REAL_INVOCATIONS targets /tmp, which is not a git repo, so `local` comes
    back null there and no producer-generated LocalStatus is ever validated
    against the schema. An in-process construction would not close that — and
    a test named for the binary that never runs it is the same defect class
    this contract exists to prevent.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    payload = _run("pull", str(tmp_path))
    assert payload["local"] is not None, "a real repo must report its status"
    assert set(payload["local"]) == set(_fixture("pull-success")["local"])
    VALIDATOR.validate(payload)


GLOBAL_FLAG_CASES = [
    (["--config", "/tmp/x.toml", "merge"], "merge", ""),
    (["--config", "/tmp/x.toml", "merge", "/repo"], "merge", "/repo"),
    # a typo'd verb must be reported as typed — scanning for "a token that
    # looks like a verb" would read `merge` out of the --slug VALUE
    (["issue-lookuq", "/repo", "--slug", "merge"], "issue-lookuq", "/repo"),
    (["--nonsense"], "unknown", ""),
    # a subcommand flag's value is a positional-looking token too: without
    # skipping it, `wanted` lands in `dir` on the wire
    (["issue-lookup", "--slug", "wanted"], "issue-lookup", ""),
    (["issue-create", "--from", "dispatcher", "/repo"], "issue-create", "/repo"),
    (["merge", "/repo", "--if-head", "abc"], "merge", "/repo"),
    (["pr-detail", "/repo", "--diff-lines", "5"], "pr-detail", "/repo"),
]


@pytest.mark.parametrize(
    "argv, action, directory", GLOBAL_FLAG_CASES, ids=lambda v: str(v)[:28]
)
def test_a_cli_error_attributes_the_verb_past_global_flags(
    capsys, argv: list[str], action: str, directory: str
) -> None:
    import github_checker.main as main_module

    with pytest.raises(SystemExit):
        main_module._refuse_argv(argv, "invalid choice")
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == action
    assert payload["dir"] == directory


def test_a_contract_error_carries_the_original_diagnosis(capsys) -> None:
    """Drift on a failing verb must not destroy the reason the verb failed —
    the operator would be told about our bug instead of about theirs."""
    from github_checker.actions import ActionResult

    import github_checker.main as main_module

    broken = ActionResult(
        action="pull", dir="/repo", ok=False, error="not a git repository"
    )
    with pytest.raises(SystemExit):
        main_module._emit(broken)
    payload = json.loads(capsys.readouterr().out)
    assert payload["result_kind"] == "contract_error"
    assert "not a git repository" in payload["error"]
