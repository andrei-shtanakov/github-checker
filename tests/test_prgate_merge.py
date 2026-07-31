"""merge is an enforcement point, not a confirmation of what the screen showed."""

import inspect
import json
import subprocess
from pathlib import Path

import pytest

from github_checker.models import CheckRun, PrDetail
from github_checker.prgate import GateUnavailable, merge_pr

HEAD = "a" * 40
OTHER = "b" * 40


def make_detail(**overrides: object) -> PrDetail:
    data: dict = {
        "number": 7,
        "title": "t",
        "url": "u",
        "state": "OPEN",
        "is_draft": False,
        "mergeable": "MERGEABLE",
        "head_branch": "feat",
        "head_sha": HEAD,
        "base_branch": "master",
        "review_decision": "APPROVED",
        "checks": [CheckRun(name="tests", state="SUCCESS")],
        "review_threads": [],
        "allows_squash": True,
    }
    data.update(overrides)
    return PrDetail(**data)


class Recorder:
    """Stands in for run_gh; records every invocation."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[tuple[str, ...]] = []
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    def __call__(self, path, *args, **kwargs):
        import subprocess

        self.calls.append(args)
        return subprocess.CompletedProcess(
            list(args), self.returncode, self.stdout, self.stderr
        )


@pytest.fixture
def patched(monkeypatch):
    def apply(detail: PrDetail, recorder: Recorder) -> None:
        monkeypatch.setattr("github_checker.prgate.pr_detail", lambda *a, **k: detail)
        monkeypatch.setattr("github_checker.prgate.run_gh", recorder)

    return apply


def test_green_pr_merges_with_squash_and_delete_branch(patched) -> None:
    rec = Recorder(stdout="merged")
    patched(make_detail(), rec)
    result = merge_pr(Path("/repo"), 7, if_head=HEAD)
    assert result.ok is True
    assert result.merged is True
    assert rec.calls, "merge must actually shell out to gh"
    argv = rec.calls[0]
    assert argv[:3] == ("pr", "merge", "7")
    assert "--squash" in argv and "--delete-branch" in argv
    # `merge` never syncs the clone; success must say so as plainly as the
    # refusal paths do, rather than leaving the field null
    assert result.local_sync == "not_attempted"


def test_head_sha_mismatch_refuses_without_calling_gh(patched) -> None:
    """TOCTOU: someone pushed between pr-detail and the click."""
    rec = Recorder()
    patched(make_detail(head_sha=OTHER), rec)
    result = merge_pr(Path("/repo"), 7, if_head=HEAD)
    assert result.ok is False
    assert result.merged is False
    assert result.gate_failed is not None
    assert "head-sha" in result.gate_failed
    assert rec.calls == [], "no mutation may be attempted on a stale head"


def test_unresolved_thread_refuses_without_calling_gh(patched) -> None:
    from github_checker.models import ReviewThread

    rec = Recorder()
    patched(make_detail(review_threads=[ReviewThread(id="t", is_resolved=False)]), rec)
    result = merge_pr(Path("/repo"), 7, if_head=HEAD)
    assert result.ok is False
    assert result.gate_failed is not None
    assert "threads-resolved" in result.gate_failed
    assert rec.calls == []


def test_draft_refuses_even_when_checks_are_green(patched) -> None:
    rec = Recorder()
    patched(make_detail(is_draft=True), rec)
    result = merge_pr(Path("/repo"), 7, if_head=HEAD)
    assert result.ok is False
    assert result.gate_failed is not None
    assert "not-draft" in result.gate_failed
    assert rec.calls == []


def test_unavailable_state_refuses(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise GateUnavailable("gh api graphql failed")

    monkeypatch.setattr("github_checker.prgate.pr_detail", boom)
    result = merge_pr(Path("/repo"), 7, if_head=HEAD)
    assert result.ok is False
    assert result.merged is False
    assert "gh api graphql failed" in (result.error or "")


def test_gh_merge_failure_leaves_merged_unknown_not_false(patched) -> None:
    """A non-zero exit from `gh pr merge` does not prove nothing happened.

    The stderr here is the case exactly: `--delete-branch` runs *after* the
    merge, so a protected branch fails the deletion while the pull request
    is already squashed. `merged=False` would be an assertion we cannot
    make; `None` says "unknown", and the caller's only safe move is to
    look again rather than merge again.
    """
    rec = Recorder(returncode=1, stderr="Protected branch update failed")
    patched(make_detail(), rec)
    result = merge_pr(Path("/repo"), 7, if_head=HEAD)
    assert result.ok is False
    assert result.merged is None
    assert result.local_sync == "not_attempted"
    assert "Protected branch" in (result.error or "")


def test_pre_launch_refusals_still_say_merged_false(patched) -> None:
    """Before `gh` is launched we know for certain nothing was merged —
    `None` there would throw away information the caller can act on."""
    rec = Recorder(returncode=0)
    patched(make_detail(state="CLOSED"), rec)
    result = merge_pr(Path("/repo"), 7, if_head=HEAD)
    assert result.ok is False
    assert result.merged is False, "a gate refusal is a definite negative"


def test_merge_binds_the_head_sha_server_side(patched) -> None:
    """The local `head_sha` comparison cannot bind: a push landing between
    that read and the merge would merge something never reviewed. GitHub
    rejecting the merge itself is the only authoritative check."""
    rec = Recorder(returncode=0)
    patched(make_detail(), rec)
    merge_pr(Path("/repo"), 7, if_head=HEAD)
    argv = " ".join(rec.calls[-1])
    assert "--match-head-commit" in argv
    assert HEAD in argv


def test_malformed_pr_view_payload_refuses_without_calling_gh(monkeypatch) -> None:
    """A KeyError inside pr_detail's parser must not escape merge_pr as a
    traceback — it must surface as the same fail-closed ActionResult as any
    other GateUnavailable. Exercises the real `pr_detail`, not a stub."""
    calls: list[tuple[str, ...]] = []

    def fake_gh(
        path: Path, *args: str, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ("pr", "view"):
            return subprocess.CompletedProcess(list(args), 0, "{}", "")
        raise AssertionError(f"unexpected gh invocation: {args}")

    monkeypatch.setattr(
        "github_checker.prgate.repo_slug", lambda path, **kw: ("acme", "widget")
    )
    monkeypatch.setattr("github_checker.prgate.run_gh", fake_gh)

    result = merge_pr(Path("/repo"), 7, if_head=HEAD)

    assert result.ok is False
    assert result.merged is False
    assert not any(call[:2] == ("pr", "merge") for call in calls)


def test_wrongly_typed_rollup_item_refuses_without_calling_gh(monkeypatch) -> None:
    """A list of wrongly-typed *items* raises AttributeError inside the
    parser, not TypeError. It must surface as the same fail-closed
    ActionResult as any other GateUnavailable, not as a raw traceback."""
    calls: list[tuple[str, ...]] = []

    def fake_gh(
        path: Path, *args: str, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ("pr", "view"):
            body = json.dumps(
                {
                    "number": 7,
                    "state": "OPEN",
                    "mergeable": "MERGEABLE",
                    "statusCheckRollup": ["ci-passed"],
                }
            )
            return subprocess.CompletedProcess(list(args), 0, body, "")
        raise AssertionError(f"unexpected gh invocation: {args}")

    monkeypatch.setattr(
        "github_checker.prgate.repo_slug", lambda path, **kw: ("acme", "widget")
    )
    monkeypatch.setattr("github_checker.prgate.run_gh", fake_gh)

    result = merge_pr(Path("/repo"), 7, if_head=HEAD)

    assert result.ok is False
    assert result.merged is False
    assert "payload" in (result.error or "")
    assert not any(call[:2] == ("pr", "merge") for call in calls)


def test_merge_pr_signature_has_no_catch_all_kwargs() -> None:
    """Pin the parameter list: a future `detail=` parameter would let a
    caller hand in stale state and reopen the TOCTOU hole this verb exists
    to close. `**kwargs` in the fixture's stub must not hide that drift."""
    params = inspect.signature(merge_pr).parameters
    assert list(params) == ["path", "number", "if_head", "binary"]
    assert all(p.kind != inspect.Parameter.VAR_KEYWORD for p in params.values())


def test_draft_and_stale_head_both_reported_together(patched) -> None:
    rec = Recorder()
    patched(make_detail(is_draft=True, head_sha=OTHER), rec)
    result = merge_pr(Path("/repo"), 7, if_head=HEAD)
    assert result.ok is False
    assert result.gate_failed is not None
    assert "not-draft" in result.gate_failed
    assert "head-sha" in result.gate_failed
    assert rec.calls == []
