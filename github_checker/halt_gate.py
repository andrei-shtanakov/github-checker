"""halt-gate: may NEW agent work start on this repository? (halt D2).

The admission rule of the DarkFactory halt, decided by one thing a
write-level token can see: the enforcement of the ruleset named
`darkfactory-halt`. (A non-admin cannot see the bypass list — D0 — so the
gate does not judge whether the ruleset is the canonical halt; dispatcher's
admin read does that and shows deviations.)

| what the reader sees                    | admit | code               |
|-----------------------------------------|-------|--------------------|
| rulesets could not be listed            | no    | refuse_unknown     |
| no ruleset of that name                 | YES   | admit_missing      |
| two or more of that name                | no    | refuse_duplicate   |
| its detail could not be read            | no    | refuse_unknown     |
| enforcement `disabled`                  | YES   | admit_off          |
| enforcement `active`                    | no    | refuse_on          |
| any other enforcement                   | no    | refuse_enforcement |

`admit_missing` is the owner's decision (2026-10-01, variant B): only an
admin can delete or disable the ruleset, and an admin bypasses the halt
anyway — a missing ruleset is a forgotten arming (a deviation dispatcher
shows), never an agent's way around the halt; refusing would block every
repository outside the fleet. Everything unreadable refuses.

The table is the contract `contracts/halt-admission/v1/` (README +
`vectors.json`); consumers in other languages are tested against the same
vectors. `decide` is the pure function those vectors drive.
"""

import json
from pathlib import Path
from typing import Any

from github_checker.actions import ActionResult, result_for
from github_checker.ghcli import repo_slug, run_gh
from github_checker.halt import HALT_RULESET

Decision = tuple[bool, str, str]

# git@github.com:o/r(.git) | ssh://git@github.com/o/r | https://github.com/o/r
_GITHUB_HOSTS = ("github.com", "www.github.com")


def is_github_origin(url: str) -> bool:
    """Whether a `remote.origin.url` points at github.com (the consumer-level
    rule: a non-GitHub checkout has no forge halt — `admit_not_github`)."""
    url = url.strip()
    if url.startswith("git@"):
        host = url[4:].split(":", 1)[0]
    elif "://" in url:
        rest = url.split("://", 1)[1]
        host = rest.split("/", 1)[0].rsplit("@", 1)[-1].split(":", 1)[0]
    else:
        return False
    return host.lower() in _GITHUB_HOSTS


def decide(
    listing: list[dict[str, Any]] | None, detail: dict[str, Any] | None
) -> Decision:
    """(admit, code, reason) from the rulesets listing and the halt's detail.

    *listing*: every ruleset as `{id, name}`, or None when it was not read.
    *detail*: the one halt ruleset's detail, or None when not read.
    """
    if listing is None:
        return False, "refuse_unknown", "rulesets could not be listed"
    named = [r for r in listing if r.get("name") == HALT_RULESET]
    if not named:
        return True, "admit_missing", "no darkfactory-halt ruleset (not armed)"
    if len(named) > 1:
        return False, "refuse_duplicate", f"{len(named)} rulesets named {HALT_RULESET}"
    if detail is None:
        return False, "refuse_unknown", "the halt ruleset could not be read"
    enforcement = detail.get("enforcement")
    if enforcement == "disabled":
        return True, "admit_off", "halt is off"
    if enforcement == "active":
        return False, "refuse_on", "the DarkFactory halt is ON for this repository"
    return False, "refuse_enforcement", f"halt enforcement {enforcement!r}"


def halt_gate(path: Path, *, binary: str = "gh") -> ActionResult:
    """May new agent work start on the repository of the clone at *path*?"""
    resolved = repo_slug(path, binary=binary)
    if resolved is None:
        return _answer(path, (False, "refuse_unknown", "repository not resolved"))
    slug = f"{resolved[0]}/{resolved[1]}"
    listing = _listing(path, slug, binary)
    detail = None
    if listing is not None:
        named = [r for r in listing if r.get("name") == HALT_RULESET]
        if len(named) == 1:
            detail = _detail(path, slug, named[0].get("id"), binary)
    return _answer(path, decide(listing, detail))


def _answer(path: Path, decision: Decision) -> ActionResult:
    admit, code, reason = decision
    unread = code == "refuse_unknown"
    return result_for(
        "halt-gate",
        path,
        # Unreadable is still a definite answer — admit is False — but the
        # read failed, so ok is False as for halt-read.
        ok=not unread,
        error=reason if unread else None,
        admit=admit,
        detail=f"{code}: {reason}",
    )


def _listing(path: Path, slug: str, binary: str) -> list[dict[str, Any]] | None:
    proc = run_gh(
        path,
        "api",
        "--paginate",
        "--slurp",
        f"repos/{slug}/rulesets?includes_parents=false",
        binary=binary,
    )
    if proc.returncode != 0:
        return None
    try:
        pages = json.loads(proc.stdout)
        return [dict(r) for page in pages for r in page]
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _detail(path: Path, slug: str, rid: object, binary: str) -> dict[str, Any] | None:
    proc = run_gh(path, "api", f"repos/{slug}/rulesets/{rid}", binary=binary)
    if proc.returncode != 0:
        return None
    try:
        body = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return body if isinstance(body, dict) else None
