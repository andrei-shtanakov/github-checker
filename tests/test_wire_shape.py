"""contracts/actions/v1: the wire shape is a property of the action.

Nothing else in the suite pins the serialised key set — every other test
asserts on the model — so the shape could drift silently while the suite
stayed green. These tests are the pin.

Semantics being frozen:
    field absent         -> the verb has no such concept
    field present, null  -> applicable, value unknown
    field present, false -> applicable, definitely negative

Key sets are asserted **whole**, and per distinguishable outcome family
rather than once per verb: a verb's empty, populated, unknown and refused
outcomes travel different code paths, and it is the path that can forget a
field.
"""

import json
import subprocess
from pathlib import Path

import pytest

from github_checker.actions import (
    ACTION_FIELDS,
    KIND_CLI_ERROR,
    KIND_CONTRACT_ERROR,
    RESULT_KINDS,
    ActionResult,
    contracted_keys,
    result_for,
    wire_fields,
)
from github_checker.models import IssueRef

ENVELOPE = {"schema_version", "result_kind", "action", "dir", "ok"}

# Spelled out, not derived from ACTION_FIELDS: a test that recomputes the map
# it checks would pass whatever the map said, mistakes included.
EXPECTED_KEYS = {
    "pull": ENVELOPE | {"error", "detail", "local"},
    "open-pr": ENVELOPE | {"error", "detail", "pr_url", "pr_state"},
    "propose-pr": ENVELOPE
    | {
        "error",
        "detail",
        "pr_url",
        "pr_state",
        "branch",
        "base_branch",
        "commit_sha",
        "changed_paths",
    },
    "pr-detail": ENVELOPE | {"error", "pr_detail"},
    "merge": ENVELOPE
    | {"error", "detail", "pr_url", "merged", "local_sync", "gate_failed", "pr_detail"},
    "post-merge-sync": ENVELOPE | {"error", "detail", "local", "branch", "local_sync"},
    "issue-lookup": ENVELOPE | {"error", "matches", "malformed"},
    "issue-create": ENVELOPE
    | {"error", "detail", "created", "issue", "matches", "malformed"},
}

REF = IssueRef(
    number=7,
    title="t",
    state="open",
    url="https://example.invalid/7",
    author="a",
    labels=["inbox"],
)


def shape(result: ActionResult) -> set[str]:
    """The key set this result actually serialises to."""
    return set(result.model_dump(mode="json", exclude_unset=True))


def test_the_eight_verbs_are_the_whole_contract() -> None:
    """`snapshot` has its own shape and belongs to snapshot/v1; the TUI
    prints no envelope at all."""
    assert set(ACTION_FIELDS) == set(EXPECTED_KEYS)
    assert "snapshot" not in ACTION_FIELDS


# --- outcome families, per verb ---------------------------------------------
#
# Each entry is one distinguishable outcome, not one verb. The four
# issue-lookup rows are the case the contract turns on: `[]` and `null` are
# different answers to the same question and must both survive the wire.
OUTCOMES = [
    ("pull", "success", {"ok": True, "detail": "already up to date"}),
    ("pull", "not a repo", {"ok": False, "error": "not a git repository"}),
    ("open-pr", "created", {"ok": True, "pr_url": "https://x/1", "pr_state": "OPEN"}),
    ("open-pr", "already open", {"ok": True, "detail": "pull request already open"}),
    ("open-pr", "created without url", {"ok": False, "error": "no PR URL"}),
    ("propose-pr", "created", {"ok": True, "branch": "b", "commit_sha": "abc"}),
    ("propose-pr", "if-match diverged", {"ok": False, "error": "if-match failed"}),
    ("pr-detail", "unavailable", {"ok": False, "error": "gate unavailable"}),
    ("merge", "merged", {"ok": True, "merged": True, "local_sync": "not_attempted"}),
    (
        "merge",
        "gate refused",
        {"ok": False, "merged": False, "gate_failed": ["checks"]},
    ),
    ("merge", "unknown after launch", {"ok": False, "merged": None}),
    ("post-merge-sync", "synced", {"ok": True, "local_sync": "ok"}),
    ("post-merge-sync", "no clone", {"ok": True, "local_sync": "not_applicable"}),
    ("post-merge-sync", "sync failed", {"ok": False, "local_sync": "failed"}),
    ("issue-lookup", "free", {"ok": True, "matches": [], "malformed": []}),
    ("issue-lookup", "one match", {"ok": True, "matches": [REF], "malformed": []}),
    ("issue-lookup", "malformed", {"ok": False, "matches": [], "malformed": [REF]}),
    ("issue-lookup", "cap hit — unread", {"ok": False, "error": "at or above the cap"}),
    ("issue-lookup", "slug refused", {"ok": False, "error": "invalid slug"}),
    ("issue-create", "created", {"ok": True, "created": True, "issue": REF}),
    ("issue-create", "slug taken", {"ok": True, "created": False, "issue": REF}),
    ("issue-create", "conflict", {"ok": False, "created": False, "matches": [REF]}),
    ("issue-create", "unknown", {"ok": False, "created": None}),
    ("issue-create", "refused pre-launch", {"ok": False, "created": False}),
]


@pytest.mark.parametrize(
    "action, family, fields",
    OUTCOMES,
    ids=[f"{a}:{f}" for a, f, _ in OUTCOMES],
)
def test_every_outcome_family_serialises_to_the_whole_key_set(
    action: str, family: str, fields: dict
) -> None:
    ok = bool(fields.pop("ok"))
    result = result_for(action, "/repo", ok=ok, **fields)
    assert shape(result) == EXPECTED_KEYS[action], (
        f"{action} / {family} does not carry its contracted shape"
    )


def test_empty_and_unknown_are_different_answers_on_the_wire() -> None:
    """`[]` means the inbox was read and holds nothing; `null` means it was
    not read exhaustively. A consumer that cannot tell them apart files a
    duplicate into an inbox it never read."""
    confirmed_empty = result_for(
        "issue-lookup", "/repo", ok=True, matches=[], malformed=[]
    )
    unread = result_for("issue-lookup", "/repo", ok=False, error="at or above the cap")
    empty_wire = confirmed_empty.model_dump(mode="json", exclude_unset=True)
    unread_wire = unread.model_dump(mode="json", exclude_unset=True)
    assert empty_wire["matches"] == []
    assert unread_wire["matches"] is None
    assert empty_wire["matches"] != unread_wire["matches"]


def test_a_verb_without_the_concept_omits_the_field_entirely() -> None:
    """The third state: not `[]`, not `null`, absent. `pull` has no inbox."""
    pulled = result_for("pull", "/repo", ok=True)
    assert "matches" not in shape(pulled)
    assert "merged" not in shape(pulled)
    assert "matches" in shape(result_for("issue-lookup", "/repo", ok=True))


def test_a_pre_dispatch_refusal_is_its_own_variant() -> None:
    """argv could not be parsed, so no verb field has a value yet — not even
    an unknown one. `action` still names the verb that was attempted."""
    assert wire_fields("no-such-verb") == frozenset({"error"})
    assert contracted_keys("no-such-verb") == ENVELOPE | {"error"}


def test_the_emit_path_ships_the_contracted_shape() -> None:
    """A real CLI invocation, not just the helper."""
    pulled = subprocess.run(
        ["uv", "run", "github-checker", "pull", "/tmp"],
        capture_output=True,
        text=True,
    )
    payload = json.loads(pulled.stdout)
    assert set(payload) == EXPECTED_KEYS["pull"]
    assert "matches" not in payload


def test_model_copy_cannot_smuggle_a_foreign_field_past_the_shape() -> None:
    """`model_copy(update=...)` adds to `model_fields_set`, so a field set
    once keeps serialising even after its value changes — including a field
    the action has no concept of."""
    pulled = result_for("pull", "/repo", ok=True)
    smuggled = pulled.model_copy(update={"merged": None})
    assert "merged" in smuggled.model_fields_set
    assert shape(smuggled) != EXPECTED_KEYS["pull"], (
        "the shape check must notice a field smuggled in by model_copy"
    )


def test_the_real_truncated_lookup_ships_the_whole_shape(monkeypatch) -> None:
    """The cap path driven through the REAL verb, not through `result_for`.

    Every other wire assertion here builds its result with the helper, so a
    production path that stopped using the helper would keep them green.
    This one takes `issue_lookup`'s own truncation branch and asserts the
    serialised key set — including `matches` present as null, which is the
    whole reason the branch refuses.
    """
    import json as _json
    import subprocess as _sp

    from github_checker.issues import ISSUE_LIST_LIMIT, issue_lookup

    payload = [
        {
            "number": n,
            "title": "t",
            "state": "OPEN",
            "url": f"https://example.invalid/{n}",
            "author": {"login": "a"},
            "labels": [{"name": "inbox"}],
            "body": f"slug: other-{n}\nfrom: x\n\np\n",
        }
        for n in range(ISSUE_LIST_LIMIT)
    ]
    monkeypatch.setattr(
        "github_checker.issues.run_gh",
        lambda *a, **k: _sp.CompletedProcess([], 0, _json.dumps(payload), ""),
    )
    monkeypatch.setattr(
        "github_checker.issues.repo_slug", lambda *a, **k: ("acme", "widget")
    )
    result = issue_lookup(Path("/repo"), "wanted")
    assert result.ok is False, "hitting the cap must fail closed"
    wire = result.model_dump(mode="json", exclude_unset=True)
    assert set(wire) == EXPECTED_KEYS["issue-lookup"]
    assert wire["matches"] is None, "unread, not confirmed-empty"


# --- the wire discriminators ------------------------------------------------


def test_every_envelope_carries_both_discriminators() -> None:
    """Without them a consumer cannot tell a deliberate minimal envelope from
    an action result that lost its required fields — both are
    `{action, dir, ok, error}` — so a schema permitting the first would
    silently accept the second."""
    for action in EXPECTED_KEYS:
        wire = shape(result_for(action, "/repo", ok=True))
        assert "schema_version" in wire
        assert "result_kind" in wire


def test_an_action_result_declares_kind_action() -> None:
    built = result_for("merge", "/repo", ok=True, merged=True)
    payload = built.model_dump(mode="json", exclude_unset=True)
    assert payload["result_kind"] == "action"
    assert payload["schema_version"] == 1


def test_the_three_kinds_are_the_whole_envelope_vocabulary() -> None:
    """`contract_error` is an envelope variant, not a ninth verb."""
    assert RESULT_KINDS == {"action", "cli_error", "contract_error"}
    assert KIND_CONTRACT_ERROR not in ACTION_FIELDS
    assert KIND_CLI_ERROR not in ACTION_FIELDS


def test_a_cli_error_names_the_attempted_verb_without_claiming_its_shape(
    capsys,
) -> None:
    """`action` is diagnostic there: it may be an unknown string, and it must
    never select an action-specific payload."""
    import github_checker.main as main_module

    with pytest.raises(SystemExit) as exit_info:
        main_module._refuse_argv(["no-such-verb", "/tmp/repo"], "invalid choice")
    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["result_kind"] == "cli_error"
    assert payload["schema_version"] == 1
    assert payload["action"] == "no-such-verb", "kept for diagnosis"
    assert set(payload) == ENVELOPE | {"error"}
    assert "merged" not in payload and "matches" not in payload


def test_a_cli_error_with_no_recognisable_verb_still_ships(capsys) -> None:
    with pytest.raises(SystemExit):
        main_module_refuse(["--nonsense"], "unrecognized arguments")
    payload = json.loads(capsys.readouterr().out)
    assert payload["result_kind"] == "cli_error"
    assert payload["action"] == "unknown"


def main_module_refuse(argv: list[str], message: str) -> None:
    import github_checker.main as main_module

    main_module._refuse_argv(argv, message)


def test_contract_error_is_its_own_leaf_and_does_not_recurse(capsys) -> None:
    """The validator has just failed on this result, so re-entering it would
    recurse or fail again. The leaf builds its payload from literals."""
    import github_checker.main as main_module

    broken = ActionResult(action="merge", dir="/repo", ok=False)  # shape short
    with pytest.raises(SystemExit) as exit_info:
        main_module._emit(broken)
    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["result_kind"] == "contract_error"
    assert payload["schema_version"] == 1
    assert payload["action"] == "merge", "diagnostic only"
    assert set(payload) == ENVELOPE | {"error"}
    assert "contract violation" in payload["error"]
    assert "merged" in payload["error"], "names what was missing"
