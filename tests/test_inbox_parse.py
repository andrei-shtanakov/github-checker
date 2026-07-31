"""The inbox body contract: grammars, parsing, canonical body."""

import pytest

from github_checker.inbox import canonical_body, slug_lines, valid_sender, valid_slug


@pytest.mark.parametrize(
    "value",
    ["a", "benchmark-2", "merge-gate-pr-listing", "x9", "a.b_c-d", "a" * 64],
)
def test_valid_slugs_are_accepted(value: str) -> None:
    assert valid_slug(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",  # empty
        "-leading-dash",  # must start alnum
        ".leading-dot",
        "Upper",  # lowercase only
        "has space",
        "has/slash",
        "a" * 65,  # 1 + 64 max
        "trailing\n",  # \Z, not $ — a trailing newline must not pass
        "two\nlines",
    ],
)
def test_invalid_slugs_are_rejected(value: str) -> None:
    assert valid_slug(value) is False


@pytest.mark.parametrize("value", ["dispatcher", "github-checker", "maestro#some-item"])
def test_valid_senders_are_accepted(value: str) -> None:
    assert valid_sender(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",
        "dispatcher\nslug: injected",  # the injection this guard exists for
        "dispatcher\r\nslug: injected",
        "dispatcher\n",
        "dispatcher\x00",
        "dispatcher#",  # '#' present but no slug
        "dispatcher#Bad",
        "Dispatcher",
    ],
)
def test_invalid_senders_are_rejected(value: str) -> None:
    assert valid_sender(value) is False


BODY = "slug: benchmark-2\nfrom: arbiter#crossover-gate\n\nNeed a second run.\n"


def test_slug_lines_reads_the_structural_block() -> None:
    assert slug_lines(BODY) == ["benchmark-2"]


def test_slug_lines_ignores_a_slug_mentioned_in_the_prose() -> None:
    body = "slug: real-one\nfrom: dispatcher\n\nAlso mentions slug: decoy here.\n"
    assert slug_lines(body) == ["real-one"]


def test_slug_lines_reports_every_structural_slug_not_just_the_first() -> None:
    body = "slug: one\nslug: two\nfrom: dispatcher\n\nprose\n"
    assert slug_lines(body) == ["one", "two"]


def test_slug_lines_is_empty_when_the_block_has_none() -> None:
    assert slug_lines("from: dispatcher\n\nprose\n") == []


def test_slug_lines_handles_a_body_with_no_blank_line() -> None:
    assert slug_lines("slug: only\nfrom: dispatcher\n") == ["only"]


def test_slug_lines_tolerates_crlf_and_surrounding_space() -> None:
    assert slug_lines("  slug:   spaced  \r\nfrom: dispatcher\r\n\r\nprose") == [
        "spaced"
    ]


def test_canonical_body_puts_the_structural_lines_first() -> None:
    out = canonical_body("my-slug", "dispatcher", "Some prose.\nSecond line.\n")
    assert out == ("slug: my-slug\nfrom: dispatcher\n\nSome prose.\nSecond line.\n")


def test_canonical_body_round_trips_through_the_parser() -> None:
    out = canonical_body("round-trip", "dispatcher", "prose")
    assert slug_lines(out) == ["round-trip"]


def test_canonical_body_rejects_an_invalid_slug() -> None:
    with pytest.raises(ValueError, match="slug"):
        canonical_body("Bad Slug", "dispatcher", "prose")


def test_canonical_body_rejects_a_sender_that_would_inject_a_line() -> None:
    with pytest.raises(ValueError, match="from"):
        canonical_body("ok-slug", "dispatcher\nslug: injected", "prose")
