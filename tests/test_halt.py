"""halt-read / halt-set: the DarkFactory halt ruleset (D0-verified shape)."""

import json
import subprocess
from pathlib import Path

import pytest

from github_checker.halt import (
    ADMIN_ONLY_BYPASS,
    canonical_ruleset,
    halt_read,
    halt_set,
)

SLUG = "acme/widget"


def _ruleset(rid: int = 11, **over: object) -> dict:
    body = canonical_ruleset("active")
    body.update(id=rid)
    body["rules"] = [{"type": "update"}]  # GitHub drops the parameters on read
    body.update(over)
    return body


class Forge:
    """A fake gh over one repository's rulesets; records every call."""

    def __init__(self, rulesets: list[dict] | None = None, *, fail: str = "") -> None:
        self.rulesets = {r["id"]: r for r in (rulesets or [])}
        self.fail = fail  # "list", "detail", "write"
        self.calls: list[tuple[str, ...]] = []
        self.bodies: list[dict] = []
        self.next_id = 99

    def __call__(self, path, *args, stdin=None, **kwargs):
        self.calls.append(args)
        done = lambda out, rc=0, err="": subprocess.CompletedProcess(  # noqa: E731
            list(args), rc, out, err
        )
        if "-X" in args:
            if self.fail == "write":
                return done("", 1, "HTTP 403")
            body = json.loads(stdin or "{}")
            self.bodies.append(body)
            endpoint = args[args.index("-X") + 2]
            rid = int(endpoint.rsplit("/", 1)[1]) if args[2] == "PUT" else self.next_id
            stored = dict(
                body, id=rid, rules=[{"type": r["type"]} for r in body["rules"]]
            )
            self.rulesets[rid] = stored
            return done(json.dumps(stored))
        endpoint = next(a for a in args if a.startswith("repos/"))
        if "rulesets?" in endpoint:
            if self.fail == "list":
                return done("", 1, "HTTP 502")
            page = [{"id": r["id"], "name": r["name"]} for r in self.rulesets.values()]
            return done(json.dumps([page]))
        if self.fail == "detail":
            return done("", 1, "HTTP 502")
        rid = int(endpoint.rsplit("/", 1)[1])
        return done(json.dumps(self.rulesets[rid]))


@pytest.fixture
def forge(monkeypatch):
    def install(f: Forge) -> Forge:
        monkeypatch.setattr("github_checker.halt.run_gh", f)
        monkeypatch.setattr(
            "github_checker.halt.repo_slug", lambda *a, **k: ("acme", "widget")
        )
        return f

    return install


def _state(result) -> str:
    assert result.halt is not None
    return result.halt.state


def test_on_off_missing(forge) -> None:
    forge(Forge([_ruleset()]))
    assert _state(halt_read(Path("/r"))) == "on"
    forge(Forge([_ruleset(enforcement="disabled")]))
    assert _state(halt_read(Path("/r"))) == "off"
    forge(Forge([]))
    result = halt_read(Path("/r"))
    assert (result.ok, _state(result)) == (True, "missing")


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        # D0: the ruleset reads "active" while master has no effective rule.
        (
            {
                "conditions": {
                    "ref_name": {"include": ["refs/heads/release"], "exclude": []}
                }
            },
            "branch condition",
        ),
        ({"target": "tag"}, "target"),
        ({"rules": [{"type": "update"}, {"type": "deletion"}]}, "rules"),
        (
            {
                "bypass_actors": ADMIN_ONLY_BYPASS
                + [
                    {
                        "actor_id": 1,
                        "actor_type": "Integration",
                        "bypass_mode": "always",
                    }
                ]
            },
            "bypass",
        ),
        # D0: a non-admin token gets `bypass_actors: null` — never "sound".
        ({"bypass_actors": None}, "needs admin"),
        ({"enforcement": "evaluate"}, "enforcement"),
    ],
)
def test_anything_but_the_canonical_halt_is_misconfigured(forge, over, needle) -> None:
    forge(Forge([_ruleset(**over)]))
    result = halt_read(Path("/r"))
    assert _state(result) == "misconfigured"
    assert result.halt is not None
    assert needle in (result.halt.detail or "")


def test_two_rulesets_of_that_name_are_misconfigured(forge) -> None:
    forge(Forge([_ruleset(1), _ruleset(2)]))
    assert _state(halt_read(Path("/r"))) == "misconfigured"


@pytest.mark.parametrize("fail", ["list", "detail"])
def test_a_failed_read_is_unknown_not_off(forge, fail) -> None:
    forge(Forge([_ruleset(enforcement="disabled")], fail=fail))
    result = halt_read(Path("/r"))
    assert (result.ok, _state(result)) == (False, "unknown")


def test_set_on_writes_the_canonical_body_and_reports_the_read_back(forge) -> None:
    f = forge(Forge([_ruleset(enforcement="disabled")]))
    result = halt_set(Path("/r"), "on")
    assert (result.ok, result.changed, _state(result)) == (True, True, "on")
    assert f.bodies == [canonical_ruleset("active")]
    assert any(
        c[:4] == ("api", "-X", "PUT", f"repos/{SLUG}/rulesets/11") for c in f.calls
    )


def test_set_off_on_a_missing_halt_arms_the_repository(forge) -> None:
    f = forge(Forge([]))
    result = halt_set(Path("/r"), "off")
    assert (result.ok, result.changed, _state(result)) == (True, True, "off")
    assert f.bodies == [canonical_ruleset("disabled")]


def test_set_repairs_a_misconfigured_single_ruleset(forge) -> None:
    bad = {"conditions": {"ref_name": {"include": ["refs/heads/x"], "exclude": []}}}
    forge(Forge([_ruleset(**bad)]))
    result = halt_set(Path("/r"), "on")
    assert (result.ok, _state(result)) == (True, "on")


def test_setting_the_state_it_already_has_is_ok_unchanged(forge) -> None:
    forge(Forge([_ruleset()]))
    result = halt_set(Path("/r"), "on")
    assert (result.ok, result.changed) == (True, False)


def test_duplicates_are_refused_not_guessed(forge) -> None:
    f = forge(Forge([_ruleset(1), _ruleset(2)]))
    result = halt_set(Path("/r"), "on")
    assert result.ok is False
    assert "resolve by hand" in (result.error or "")
    assert f.bodies == []


def test_an_unknown_read_refuses_to_write(forge) -> None:
    f = forge(Forge([_ruleset()], fail="list"))
    result = halt_set(Path("/r"), "off")
    assert (result.ok, _state(result)) == (False, "unknown")
    assert f.bodies == []


def test_a_failed_write_reports_what_was_read_back(forge) -> None:
    forge(Forge([_ruleset(enforcement="disabled")], fail="write"))
    result = halt_set(Path("/r"), "on")
    assert (result.ok, result.changed, _state(result)) == (False, False, "off")
    assert "403" in (result.error or "")


def test_a_write_the_read_back_contradicts_is_not_ok(forge, monkeypatch) -> None:
    """A write that "succeeded" but reads back otherwise (e.g. a non-admin
    who cannot see the bypass list) is not a halt."""
    f = forge(Forge([_ruleset(enforcement="disabled")]))
    real = f.__call__

    def hide_bypass(path, *args, **kw):
        out = real(path, *args, **kw)
        if "-X" not in args and "rulesets/" in args[-1] and "?" not in args[-1]:
            body = json.loads(out.stdout)
            body["bypass_actors"] = None
            out = subprocess.CompletedProcess(out.args, 0, json.dumps(body), "")
        return out

    monkeypatch.setattr("github_checker.halt.run_gh", hide_bypass)
    result = halt_set(Path("/r"), "on")
    assert result.ok is False
    assert "read back misconfigured" in (result.error or "")


def test_an_unresolvable_repository_is_unknown(forge, monkeypatch) -> None:
    forge(Forge([]))
    monkeypatch.setattr("github_checker.halt.repo_slug", lambda *a, **k: None)
    assert _state(halt_read(Path("/r"))) == "unknown"
    assert _state(halt_set(Path("/r"), "on")) == "unknown"


def test_a_write_whose_read_back_fails_has_changed_unknown(forge, monkeypatch) -> None:
    """Review #50: the write landed (rc 0) but the read-back failed — no
    claim about movement can be made."""
    f = forge(Forge([_ruleset(enforcement="disabled")]))
    real = f.__call__
    wrote = []

    def fail_after_write(path, *args, **kw):
        if "-X" in args:
            wrote.append(1)
            return real(path, *args, **kw)
        if wrote:
            return subprocess.CompletedProcess(list(args), 1, "", "HTTP 502")
        return real(path, *args, **kw)

    monkeypatch.setattr("github_checker.halt.run_gh", fail_after_write)
    result = halt_set(Path("/r"), "on")
    assert (result.ok, result.changed, _state(result)) == (False, None, "unknown")


@pytest.mark.parametrize(
    "over",
    [{"conditions": None}, {"conditions": {}}, {"rules": None}, {"target": None}],
)
def test_a_halt_named_ruleset_missing_parts_is_misconfigured(forge, over) -> None:
    """Review #50: absent parts are not an unreadable answer — and the
    repair path (halt-set) must then be able to overwrite it."""
    forge(Forge([_ruleset(**over)]))
    assert _state(halt_read(Path("/r"))) == "misconfigured"
    assert _state(halt_set(Path("/r"), "on")) == "on"


def test_the_cli_refusal_is_a_definite_no_change(capsys, monkeypatch) -> None:
    """Review #50: a missing --state ran nothing — changed is False."""
    import sys

    from github_checker import main as cli

    monkeypatch.setattr(sys, "argv", ["github-checker", "halt-set", "/tmp"])
    with pytest.raises(SystemExit):
        cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert (payload["ok"], payload["changed"]) == (False, False)
