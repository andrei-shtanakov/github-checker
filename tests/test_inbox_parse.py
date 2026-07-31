"""The inbox body contract: grammars, parsing, canonical body."""

import pytest

from github_checker.inbox import valid_sender, valid_slug


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
