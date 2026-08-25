"""epics/v1 classifier: trailer parsing, four-state output, KB conformance.

The behavioral spec lives in the vendored contract
(`github_checker/contract_epics/README.md`); the fixture replays at the bottom
are the machine-checkable half — a conforming classifier reproduces the
pinned `expected.json` objects for the pull_request/issue carriers.
"""

import json
from pathlib import Path

from github_checker.epics import (
    EpicClassification,
    artifact_uri,
    classify_body,
)

FIXTURES = Path(__file__).parent.parent / "github_checker" / "contract_epics"


def classify(body: str | None, *, retrieved: bool = True) -> EpicClassification:
    return classify_body(
        body,
        retrieved=retrieved,
        subject_uri="gh://demo/pull/7",
        carrier="pull_request",
        observed_at="2026-08-25T00:00:00Z",
    )


def codes(result: EpicClassification) -> list[str]:
    return [d.code for d in result.diagnostics]


# ---------------------------------------------------------------- epic axis


def test_tagged_from_final_trailer_block() -> None:
    result = classify("Does a thing.\n\nEpic: eco.ops\n")
    assert result.classification == "tagged"
    assert result.epic == "eco.ops"
    assert result.defect is None
    assert result.diagnostics == []


def test_missing_when_no_trailer_block() -> None:
    result = classify("Just prose, no trailers at all.")
    assert result.classification == "missing"
    assert result.epic is None
    assert codes(result) == ["EP-MISSING"]
    assert result.diagnostics[0].severity == "warning"
    assert result.diagnostics[0].message == "artifact carries no Epic trailer"
    assert result.diagnostics[0].subject_uri == "gh://demo/pull/7"


def test_null_body_is_read_and_missing_not_unavailable() -> None:
    result = classify(None)
    assert result.classification == "missing"
    assert codes(result) == ["EP-MISSING"]


def test_grammar_failure_keeps_raw() -> None:
    result = classify("Fix.\n\nEpic: Eco.Ops\n")
    assert result.classification == "invalid"
    assert result.epic is None
    assert codes(result) == ["EP-GRAMMAR"]
    assert result.diagnostics[0].severity == "error"
    assert result.diagnostics[0].raw == "Eco.Ops"
    assert (
        result.diagnostics[0].message
        == "Epic 'Eco.Ops' does not match <program>.<epic> (epics/v1)"
    )


def test_no_dot_is_grammar_failure() -> None:
    assert codes(classify("x\n\nEpic: eco\n")) == ["EP-GRAMMAR"]


def test_two_epics_are_multiple_even_when_identical() -> None:
    result = classify("x\n\nEpic: eco.ops\nEpic: eco.ops\n")
    assert result.classification == "invalid"
    assert result.epic is None
    assert codes(result) == ["EP-MULTIPLE"]
    assert (
        result.diagnostics[0].message
        == "artifact carries 2 Epic trailers (eco.ops, eco.ops)"
    )


# ------------------------------------------------------------ trailer block


def test_mid_prose_mention_is_not_a_trailer() -> None:
    body = "Epic: eco.ops is discussed here\nand more prose follows.\n"
    assert classify(body).classification == "missing"


def test_final_paragraph_with_a_non_trailer_line_is_not_a_block() -> None:
    body = "x\n\nEpic: eco.ops\nthis line breaks the block\n"
    assert classify(body).classification == "missing"


def test_crlf_bodies_parse() -> None:
    result = classify("Does a thing.\r\n\r\nEpic: eco.ops\r\nDefect: pipeline\r\n")
    assert result.classification == "tagged"
    assert result.epic == "eco.ops"
    assert result.defect == "pipeline"


def test_trailer_value_whitespace_is_stripped_not_case_folded() -> None:
    assert classify("x\n\nEpic:  eco.ops \n").epic == "eco.ops"


def test_key_match_is_case_sensitive() -> None:
    # `epic:` is a foreign trailer key, not a lower-cased synonym.
    assert classify("x\n\nepic: eco.ops\n").classification == "missing"


def test_other_trailers_alongside_are_ignored() -> None:
    body = "x\n\nEpic: eco.ops\nCo-Authored-By: A <a@b.c>\n"
    assert classify(body).epic == "eco.ops"


# --------------------------------------------------------------- defect axis


def test_defect_grammar_failure_does_not_touch_epic_axis() -> None:
    result = classify("x\n\nEpic: eco.ops\nDefect: Not_Valid\n")
    assert result.classification == "tagged"
    assert result.epic == "eco.ops"
    assert result.defect is None
    assert codes(result) == ["EP-DEFECT-GRAMMAR"]
    assert result.diagnostics[0].raw == "Not_Valid"
    assert result.diagnostics[0].message == (
        "Defect 'Not_Valid' does not match the defect slug grammar (epics/v1)"
    )


def test_two_defects_are_multiple() -> None:
    result = classify("x\n\nEpic: eco.ops\nDefect: code\nDefect: code\n")
    assert result.defect is None
    assert codes(result) == ["EP-DEFECT-MULTIPLE"]
    assert (
        result.diagnostics[0].message
        == "artifact carries 2 Defect trailers (code, code)"
    )


def test_defect_alone_still_leaves_epic_missing() -> None:
    result = classify("x\n\nDefect: pipeline\n")
    assert result.classification == "missing"
    assert result.defect == "pipeline"
    assert codes(result) == ["EP-MISSING"]


def test_diagnostics_are_sorted_by_code() -> None:
    result = classify("x\n\nEpic: eco\nDefect: Bad_Slug\n")
    assert codes(result) == ["EP-DEFECT-GRAMMAR", "EP-GRAMMAR"]


# --------------------------------------------------------------- unavailable


def test_unavailable_never_missing() -> None:
    result = classify("whatever the transport left behind", retrieved=False)
    assert result.classification == "unavailable"
    assert result.epic is None
    assert result.defect is None
    assert codes(result) == ["EP-UNAVAILABLE"]
    assert result.diagnostics[0].severity == "warning"
    assert result.diagnostics[0].message == "artifact body was not retrieved"


# --------------------------------------------------------------- subject_uri


def test_artifact_uri_uses_short_repo_name() -> None:
    assert (
        artifact_uri("andrei-shtanakov/github-checker", "pull", 7)
        == "gh://github-checker/pull/7"
    )
    assert (
        artifact_uri("andrei-shtanakov/github-checker", "issues", 11)
        == "gh://github-checker/issues/11"
    )


def test_artifact_uri_outside_grammar_is_null_not_a_lie() -> None:
    assert artifact_uri("owner/Repo.With.Dots", "pull", 7) is None


# ---------------------------------------------------- KB fixture conformance


def _dump(result: EpicClassification) -> dict:
    got = result.model_dump()
    for diag in got["diagnostics"]:
        if diag["raw"] is None:
            del diag["raw"]
    return got


def _expected(name: str) -> dict:
    return json.loads((FIXTURES / "fixtures" / name).read_text(encoding="utf-8"))


def test_kb_fixture_pr_body() -> None:
    body = (FIXTURES / "fixtures" / "valid" / "pr-body.txt").read_text()
    result = classify_body(
        body,
        retrieved=True,
        subject_uri="gh://demo/pull/7",
        carrier="pull_request",
        observed_at="2026-08-25T00:00:00Z",
    )
    assert _dump(result) == _expected("valid/pr-body.expected.json")


def test_kb_fixture_prose_mention_is_not_a_trailer() -> None:
    body = (
        FIXTURES / "fixtures" / "valid" / "prose-mention-is-not-a-trailer.txt"
    ).read_text()
    result = classify_body(
        body,
        retrieved=True,
        subject_uri="gh://demo/issues/11",
        carrier="issue",
        observed_at="2026-08-25T00:00:00Z",
    )
    assert _dump(result) == _expected(
        "valid/prose-mention-is-not-a-trailer.expected.json"
    )


def test_kb_fixture_issue_unavailable() -> None:
    context = _expected("invalid/issue-unavailable/context.json")
    assert context["body_retrieved"] is False
    result = classify_body(
        None,
        retrieved=False,
        subject_uri=context["subject_uri"],
        carrier=context["carrier"],
        observed_at=context["observed_at"],
    )
    assert _dump(result) == _expected("invalid/issue-unavailable/expected.json")
