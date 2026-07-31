"""The ADR-ECO-006 inbox body contract, as pure text rules.

No `gh` and no I/O here on purpose: the subtle parts of this feature are
textual — exact slug equality, one structural `slug:` line, a sender that
cannot inject body lines — and they are worth testing without a fake
subprocess in the way.
"""

import re

# ADR-ECO-005 PF-2B. `\Z` not `$`: under `fullmatch` both reject a trailing
# newline, but `$` matches just before one — so switching to `.match()` or
# `.search()` later would silently start accepting "dispatcher\n", the exact
# value the injection guard below exists to reject.
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


_SLUG_LINE_RE = re.compile(r"\A\s*slug:\s*(\S+)\s*\Z")


def slug_lines(body: str) -> list[str]:
    """Values of every `slug:` line in the body's structural block.

    The block is the leading lines up to the first blank one (ADR-ECO-006
    D3). Scanning only it is what keeps a `slug:` written inside the prose
    from being read as identity. The count is meaningful to the caller:
    two claims are malformed, not a first-wins choice.
    """
    values: list[str] = []
    for raw in body.replace("\r\n", "\n").split("\n"):
        if not raw.strip():
            break
        match = _SLUG_LINE_RE.match(raw)
        if match:
            values.append(match.group(1))
    return values


def canonical_body(slug: str, sender: str, prose: str) -> str:
    """Build an ADR-ECO-006 D3 body: structural block, blank line, prose.

    The caller supplies prose only — the structural lines are ours to
    write, so they cannot be spoofed by what a form submitted.
    """
    if not valid_slug(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    if not valid_sender(sender):
        raise ValueError(f"invalid from: {sender!r}")
    return f"slug: {slug}\nfrom: {sender}\n\n{prose}"
