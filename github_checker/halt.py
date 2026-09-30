"""halt-read / halt-set: the DarkFactory halt ("stop-crane") on one repository.

The halt is a repository ruleset named `darkfactory-halt` on the default
branch with one rule, `update` (restrict updates), and a bypass list of
exactly the repository admin role — verified by execution on a sandbox
(dispatcher spec 2026-09-29-human-control-plane-design §6.4, D0, 2026-09-30):
while it is active, GitHub refuses every update of the default branch to
every actor without bypass — the agent's merge, a direct push, an Actions
token — and admits the admin.

`halt-read` reports one of five states. Only `on` and `off` are healthy:

- `on` / `off` — the ruleset exists, matches the canonical definition, and
  is `active` / `disabled`;
- `missing` — no ruleset of that name: the repository was never armed;
- `misconfigured` — it exists but is not the halt: a duplicate name, another
  target or branch condition (D0: a ruleset on the wrong branch reads
  "active" while the default branch has no effective rule), another rule, or
  a bypass list other than the admin role — including a list this reader
  cannot SEE: GitHub returns `bypass_actors: null` to a non-admin, so a
  write-level token can never confirm a halt is sound;
- `unknown` — the read itself failed.

The unit of truth is the ruleset read back, never the write's answer:
`halt-set` writes the canonical definition (which also repairs a
misconfigured single ruleset), then reads the halt again and reports THAT.
`halt-set --state off` on a repository with no ruleset creates it disabled —
"arming" the repository, so a later halt is a toggle and admission checks
can see a confirmed `off` instead of `missing`.
"""

import json
from pathlib import Path
from typing import Any, Literal

from github_checker.actions import ActionResult, result_for
from github_checker.ghcli import repo_slug, run_gh
from github_checker.models import HaltStatus

HALT_RULESET = "darkfactory-halt"
#: Repository role id of `admin` in ruleset bypass lists.
ADMIN_ROLE_ID = 5
ADMIN_ONLY_BYPASS = [
    {"actor_id": ADMIN_ROLE_ID, "actor_type": "RepositoryRole", "bypass_mode": "always"}
]


def canonical_ruleset(enforcement: Literal["active", "disabled"]) -> dict[str, Any]:
    """The one definition of the halt; `halt-set` writes exactly this."""
    return {
        "name": HALT_RULESET,
        "target": "branch",
        "enforcement": enforcement,
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "update", "parameters": {"update_allows_fetch_and_merge": False}}
        ],
        "bypass_actors": ADMIN_ONLY_BYPASS,
    }


def halt_read(path: Path, *, binary: str = "gh") -> ActionResult:
    """The halt state of the repository of the clone at *path*."""
    slug = _slug(path, binary)
    if slug is None:
        return result_for(
            "halt-read",
            path,
            ok=False,
            error="cannot resolve owner/repo for this clone",
            halt=HaltStatus(state="unknown", detail="repository not resolved"),
        )
    status = read_status(path, slug, binary)
    return result_for(
        "halt-read",
        path,
        ok=status.state != "unknown",
        error=status.detail if status.state == "unknown" else None,
        halt=status,
    )


def halt_set(
    path: Path, state: Literal["on", "off"], *, binary: str = "gh"
) -> ActionResult:
    """Write the canonical halt with *state*, then read it back."""
    slug = _slug(path, binary)
    if slug is None:
        return result_for(
            "halt-set",
            path,
            ok=False,
            error="cannot resolve owner/repo for this clone",
            changed=False,
            halt=HaltStatus(state="unknown", detail="repository not resolved"),
        )
    before, ids = _read(path, slug, binary)
    if before.state == "unknown":
        return result_for(
            "halt-set", path, ok=False, error=before.detail, changed=False, halt=before
        )
    if len(ids) > 1:
        # Which one is "the" halt is a human's call, not a write's.
        return result_for(
            "halt-set",
            path,
            ok=False,
            error=f"{len(ids)} rulesets named {HALT_RULESET}; resolve by hand",
            changed=False,
            halt=before,
        )
    enforcement: Literal["active", "disabled"] = (
        "active" if state == "on" else "disabled"
    )
    body = json.dumps(canonical_ruleset(enforcement))
    endpoint = f"repos/{slug}/rulesets" + (f"/{ids[0]}" if ids else "")
    method = "PUT" if ids else "POST"
    proc = run_gh(
        path, "api", "-X", method, endpoint, "--input", "-", binary=binary, stdin=body
    )
    after = read_status(path, slug, binary)
    if proc.returncode != 0:
        return result_for(
            "halt-set",
            path,
            ok=False,
            error=proc.stderr.strip() or f"{method} {endpoint} failed",
            # The write failed, but it may still have landed: say what the
            # read-back saw rather than assuming nothing changed.
            changed=_moved(before, after),
            halt=after,
        )
    ok = after.state == state
    return result_for(
        "halt-set",
        path,
        ok=ok,
        error=None if ok else f"wrote {state} but read back {after.state}",
        changed=_moved(before, after),
        halt=after,
    )


def _moved(before: HaltStatus, after: HaltStatus) -> bool | None:
    """Did the read-back state move? None when the read-back is unknown —
    after a write, an unread state proves nothing either way (review #50)."""
    return None if after.state == "unknown" else after.state != before.state


def read_status(path: Path, slug: str, binary: str) -> HaltStatus:
    """The halt state of *slug*; never raises."""
    return _read(path, slug, binary)[0]


def _slug(path: Path, binary: str) -> str | None:
    resolved = repo_slug(path, binary=binary)
    return None if resolved is None else f"{resolved[0]}/{resolved[1]}"


def _read(path: Path, slug: str, binary: str) -> tuple[HaltStatus, list[int]]:
    """(status, ids of every ruleset named like the halt)."""
    proc = run_gh(
        path,
        "api",
        "--paginate",
        "--slurp",
        f"repos/{slug}/rulesets?includes_parents=false",
        binary=binary,
    )
    if proc.returncode != 0:
        return _unknown(proc.stderr.strip() or "listing rulesets failed"), []
    try:
        pages = json.loads(proc.stdout)
        ids = [
            int(r["id"]) for page in pages for r in page if r["name"] == HALT_RULESET
        ]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as err:
        return _unknown(f"unexpected rulesets payload: {err!r}"), []
    if not ids:
        return HaltStatus(
            state="missing", detail="no ruleset named darkfactory-halt"
        ), []
    if len(ids) > 1:
        return (
            HaltStatus(
                state="misconfigured", detail=f"{len(ids)} rulesets named alike"
            ),
            ids,
        )
    proc = run_gh(path, "api", f"repos/{slug}/rulesets/{ids[0]}", binary=binary)
    if proc.returncode != 0:
        return _unknown(proc.stderr.strip() or "reading the ruleset failed"), ids
    try:
        detail = json.loads(proc.stdout)
        return _judge(detail, ids[0]), ids
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as err:
        return _unknown(f"unexpected ruleset payload: {err!r}"), ids


def _judge(ruleset: dict[str, Any], ruleset_id: int) -> HaltStatus:
    """Compare one ruleset with the canonical halt.

    Absent parts are read defensively (as github.py does for the same
    endpoint): a ruleset of that name with no conditions or no rules is not
    the halt — `misconfigured`, which halt-set can overwrite — never an
    unreadable `unknown` (review #50).
    """
    problems: list[str] = []
    if ruleset.get("target") != "branch":
        problems.append(f"target {ruleset.get('target')!r}")
    ref = (ruleset.get("conditions") or {}).get("ref_name") or {}
    if ref.get("include") != ["~DEFAULT_BRANCH"] or ref.get("exclude") not in (
        [],
        None,
    ):
        problems.append(f"branch condition {ref or 'missing'}")
    types = sorted(rule.get("type") for rule in ruleset.get("rules") or [])
    if types != ["update"]:
        problems.append(f"rules {types}")
    bypass = ruleset.get("bypass_actors")
    if bypass is None:
        problems.append("bypass list not visible to this reader (needs admin)")
    elif [
        {k: b.get(k) for k in ("actor_id", "actor_type", "bypass_mode")} for b in bypass
    ] != ADMIN_ONLY_BYPASS:
        problems.append(f"bypass {bypass}")
    if problems:
        return HaltStatus(
            state="misconfigured", ruleset_id=ruleset_id, detail="; ".join(problems)
        )
    enforcement = ruleset.get("enforcement")
    if enforcement == "active":
        return HaltStatus(state="on", ruleset_id=ruleset_id)
    if enforcement == "disabled":
        return HaltStatus(state="off", ruleset_id=ruleset_id)
    return HaltStatus(
        state="misconfigured",
        ruleset_id=ruleset_id,
        detail=f"enforcement {enforcement!r}",
    )


def _unknown(detail: str) -> HaltStatus:
    return HaltStatus(state="unknown", detail=detail)
