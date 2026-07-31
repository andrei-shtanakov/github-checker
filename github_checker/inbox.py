"""The ADR-ECO-006 inbox body contract, as pure text rules.

No `gh` and no I/O here on purpose: the subtle parts of this feature are
textual — exact slug equality, one structural `slug:` line, a sender that
cannot inject body lines — and they are worth testing without a fake
subprocess in the way.
"""

import re

# ADR-ECO-005 PF-2B. `\Z` not `$`: `$` matches before a trailing newline,
# so a `$`-anchored pattern would accept "dispatcher\n" — exactly the
# value this rejects elsewhere.
SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")
SENDER_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}(#[a-z0-9][a-z0-9._-]{0,63})?\Z")


def valid_slug(value: str) -> bool:
    """True if *value* is a canonical plan-item slug (ADR-ECO-005 PF-2B)."""
    return bool(SLUG_RE.fullmatch(value))


def valid_sender(value: str) -> bool:
    """True if *value* is a valid `from:` — repo name, optionally `#slug`.

    Rejects CR/LF and control characters: this value is written into the
    body's structural block, so a newline would append arbitrary lines
    there — a second `slug:` included.
    """
    return bool(SENDER_RE.fullmatch(value))
