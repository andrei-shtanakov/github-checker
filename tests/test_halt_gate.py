"""halt-gate: the halt admission rule (contracts/halt-admission/v1)."""

import json
import subprocess
from pathlib import Path

import pytest

from github_checker.halt_gate import decide, halt_gate

VECTORS = json.loads(
    (
        Path(__file__).parent.parent / "contracts/halt-admission/v1/vectors.json"
    ).read_text()
)["vectors"]
CODES = {
    "refuse_unknown",
    "admit_missing",
    "refuse_duplicate",
    "admit_off",
    "refuse_on",
    "refuse_enforcement",
}


@pytest.mark.parametrize("v", VECTORS, ids=[v["name"] for v in VECTORS])
def test_every_vector(v: dict) -> None:
    admit, code, _ = decide(v["listing"], v["detail"])
    assert (admit, code) == (v["admit"], v["code"])


def test_the_vectors_cover_every_code() -> None:
    assert {v["code"] for v in VECTORS} == CODES


class Forge:
    def __init__(self, listing=None, details=None, fail: str = "") -> None:
        self.listing = listing if listing is not None else []
        self.details = details or {}
        self.fail = fail
        self.calls: list[tuple] = []

    def __call__(self, path, *args, **kw):
        self.calls.append(args)
        done = lambda out, rc=0: subprocess.CompletedProcess(list(args), rc, out, "x")  # noqa: E731
        endpoint = args[-1]
        if "rulesets?" in endpoint:
            return (
                done("", 1) if self.fail == "list" else done(json.dumps([self.listing]))
            )
        if self.fail == "detail":
            return done("", 1)
        return done(json.dumps(self.details[int(endpoint.rsplit("/", 1)[1])]))


@pytest.fixture
def forge(monkeypatch):
    def install(f: Forge, slug=("acme", "widget")) -> Forge:
        monkeypatch.setattr("github_checker.halt_gate.run_gh", f)
        monkeypatch.setattr("github_checker.halt_gate.repo_slug", lambda *a, **k: slug)
        return f

    return install


def test_halt_on_refuses(forge) -> None:
    f = forge(
        Forge([{"id": 7, "name": "darkfactory-halt"}], {7: {"enforcement": "active"}})
    )
    r = halt_gate(Path("/r"))
    assert (r.ok, r.admit) == (True, False)
    assert (r.detail or "").startswith("refuse_on")
    assert any(c[-1] == "repos/acme/widget/rulesets/7" for c in f.calls)


def test_halt_off_and_missing_admit(forge) -> None:
    forge(
        Forge([{"id": 7, "name": "darkfactory-halt"}], {7: {"enforcement": "disabled"}})
    )
    assert halt_gate(Path("/r")).admit is True
    forge(Forge([]))
    r = halt_gate(Path("/r"))
    assert r.admit is True and (r.detail or "").startswith("admit_missing")


@pytest.mark.parametrize("fail", ["list", "detail"])
def test_unreadable_refuses_and_is_not_ok(forge, fail) -> None:
    forge(
        Forge(
            [{"id": 7, "name": "darkfactory-halt"}],
            {7: {"enforcement": "disabled"}},
            fail=fail,
        )
    )
    r = halt_gate(Path("/r"))
    assert (r.ok, r.admit) == (False, False)
    assert (r.detail or "").startswith("refuse_unknown")


def test_duplicates_refuse_without_reading_details(forge) -> None:
    f = forge(
        Forge(
            [
                {"id": 1, "name": "darkfactory-halt"},
                {"id": 2, "name": "darkfactory-halt"},
            ]
        )
    )
    r = halt_gate(Path("/r"))
    assert r.admit is False
    assert len(f.calls) == 1


def test_an_unresolvable_repository_refuses(forge) -> None:
    forge(Forge([]), slug=None)
    r = halt_gate(Path("/r"))
    assert (r.ok, r.admit) == (False, False)


ORIGINS = json.loads(
    (
        Path(__file__).parent.parent / "contracts/halt-admission/v1/vectors.json"
    ).read_text()
)["origin_vectors"]


@pytest.mark.parametrize("v", ORIGINS, ids=[v["origin"] or "<empty>" for v in ORIGINS])
def test_every_origin_vector(v: dict) -> None:
    from github_checker.halt_gate import is_github_origin

    assert is_github_origin(v["origin"]) is v["github"]
