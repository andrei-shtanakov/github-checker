import json
import subprocess
from pathlib import Path

import pytest

import github_checker.main as main_module
from github_checker import actions


def test_main_exits_on_corrupt_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "repos.toml"
    config_path.write_text("repos = [", encoding="utf-8")
    monkeypatch.setattr(main_module, "gh_ready", lambda: None)
    monkeypatch.setattr("sys.argv", ["github-checker", "--config", str(config_path)])
    with pytest.raises(SystemExit) as excinfo:
        main_module.main()
    assert excinfo.value.code == 1
    assert "repos.toml" in capsys.readouterr().err


def test_main_exits_when_gh_not_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main_module, "gh_ready", lambda: "gh не авторизован")
    monkeypatch.setattr("sys.argv", ["github-checker"])
    with pytest.raises(SystemExit) as excinfo:
        main_module.main()
    assert excinfo.value.code == 1
    assert "gh не авторизован" in capsys.readouterr().err


def _git(path: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def _make_pair(tmp_path: Path) -> Path:
    """A bare origin + a local clone, seeded with one committed file."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "project.yaml").write_text("spec_runner:\n  max_retries: 3\n")
    _git(seed, "add", "project.yaml")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)],
        check=True,
        capture_output=True,
    )
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    return clone


def _main_exit_code() -> int:
    """Run main() and return its exit code; 0 when it returns without SystemExit."""
    try:
        main_module.main()
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    return 0


def test_main_propose_pr_happy_path_prints_ok_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clone = _make_pair(tmp_path)
    monkeypatch.setattr(
        actions,
        "run_gh",
        lambda path, *args: subprocess.CompletedProcess(
            ["gh", *args], 0, stdout="https://github.com/o/r/pull/7\n"
        ),
    )
    content = tmp_path / "new.yaml"
    content.write_text("spec_runner:\n  max_retries: 9\n")
    monkeypatch.setattr(
        "sys.argv",
        [
            "github-checker",
            "propose-pr",
            str(clone),
            "--message",
            "bump retries",
            "--edit",
            f"project.yaml={content}",
        ],
    )
    assert _main_exit_code() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "propose-pr"
    assert payload["ok"] is True
    assert payload["pr_url"] == "https://github.com/o/r/pull/7"
    assert payload["branch"]
    assert payload["base_branch"] == "main"
    assert payload["commit_sha"]
    assert payload["changed_paths"] == ["project.yaml"]


def test_main_propose_pr_without_edit_exits_one_with_json_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clone = _make_pair(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["github-checker", "propose-pr", str(clone), "--message", "no edits"],
    )
    assert _main_exit_code() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "at least one --edit" in payload["error"]


def test_merge_without_if_head_returns_json_not_a_usage_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["github-checker", "merge", "/tmp/repo", "7"])
    with pytest.raises(SystemExit) as exit_info:
        main_module.main()
    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "--if-head" in payload["error"]
    # Every other merge refusal goes through prgate._merge_failure, which
    # sets this; this path must match so a JSON consumer never has to treat
    # a null local_sync as a special case.
    assert payload["local_sync"] == "not_attempted"


@pytest.mark.parametrize("flag", ["--file-limit", "--diff-lines"])
@pytest.mark.parametrize("value", ["-5", "0"])
def test_pr_detail_rejects_non_positive_limits(
    flag: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["github-checker", "pr-detail", "/tmp/repo", "7", flag, value],
    )
    with pytest.raises(SystemExit) as exit_info:
        main_module.main()
    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert flag in payload["error"]


def test_pr_detail_prints_the_detail_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from github_checker.models import PrDetail

    detail = PrDetail(
        number=7,
        title="t",
        url="u",
        state="OPEN",
        is_draft=False,
        mergeable="MERGEABLE",
        head_branch="feat",
        head_sha="a" * 40,
        base_branch="master",
    )
    monkeypatch.setattr("github_checker.prgate.pr_detail", lambda *a, **k: detail)
    monkeypatch.setattr("sys.argv", ["github-checker", "pr-detail", "/tmp/repo", "7"])
    main_module.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["pr_detail"]["number"] == 7


def test_pr_detail_reports_unavailable_state_as_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from github_checker.prgate import GateUnavailable

    def boom(*args: object, **kwargs: object) -> None:
        raise GateUnavailable("gh pr view failed")

    monkeypatch.setattr("github_checker.prgate.pr_detail", boom)
    monkeypatch.setattr("sys.argv", ["github-checker", "pr-detail", "/tmp/repo", "7"])
    with pytest.raises(SystemExit) as exit_info:
        main_module.main()
    assert exit_info.value.code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_merge_refusal_strips_the_diff_before_printing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from github_checker.actions import result_for
    from github_checker.models import PrDetail

    detail = PrDetail(
        number=7,
        title="t",
        url="u",
        state="OPEN",
        is_draft=False,
        mergeable="MERGEABLE",
        head_branch="feat",
        head_sha="a" * 40,
        base_branch="master",
        diff="+huge diff body\n" * 500,
    )
    # built through result_for so the fixture carries the verb's contracted
    # shape: a hand-written ActionResult would be missing applicable fields
    # and _emit would (correctly) refuse it as a contract violation
    refusal = result_for(
        "merge",
        "/tmp/repo",
        ok=False,
        merged=False,
        gate_failed=["checks"],
        error="merge gate refused: checks",
        pr_detail=detail,
    )
    monkeypatch.setattr("github_checker.prgate.merge_pr", lambda *a, **k: refusal)
    monkeypatch.setattr(
        "sys.argv",
        ["github-checker", "merge", "/tmp/repo", "7", "--if-head", "a" * 40],
    )
    with pytest.raises(SystemExit) as exit_info:
        main_module.main()
    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["gate_failed"] == ["checks"]
    assert payload["pr_detail"]["diff"] is None
    # The strip must be a copy, not a mutation: the source PrDetail that
    # prgate.merge_pr() (and anything else holding a reference to it) built
    # is untouched by what the CLI chooses to print.
    assert detail.diff is not None


def test_unexpected_exception_still_prints_json_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("github_checker.prgate.pr_detail", boom)
    monkeypatch.setattr("sys.argv", ["github-checker", "pr-detail", "/tmp/repo", "7"])
    with pytest.raises(SystemExit) as exit_info:
        main_module.main()
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert "RuntimeError" in payload["error"]
    assert "boom" in payload["error"]
    # The frame list still goes to stderr, outside the JSON contract, so a
    # real bug stays locatable instead of just "some exception happened".
    assert "Traceback" in captured.err
    assert "RuntimeError" in captured.err


def test_pull_unexpected_exception_still_prints_json_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The guard is not special-cased to the three new verbs — pre-existing
    verbs (pull/open-pr/propose-pr) now share the same "always JSON" contract.
    """

    def boom(directory: Path) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("github_checker.actions.pull", boom)
    monkeypatch.setattr("sys.argv", ["github-checker", "pull", "/tmp/repo"])
    with pytest.raises(SystemExit) as exit_info:
        main_module.main()
    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["action"] == "pull"
    assert "RuntimeError" in payload["error"]


def test_post_merge_sync_wiring_prints_the_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from github_checker.actions import result_for

    result = result_for(
        "post-merge-sync",
        "/tmp/repo",
        ok=True,
        local_sync="ok",
        detail="fast-forwarded",
    )
    monkeypatch.setattr(
        "github_checker.actions.post_merge_sync", lambda *a, **k: result
    )
    monkeypatch.setattr("sys.argv", ["github-checker", "post-merge-sync", "/tmp/repo"])
    main_module.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "post-merge-sync"
    assert payload["ok"] is True
    assert payload["local_sync"] == "ok"


def test_issue_create_without_from_returns_json_not_a_usage_error(
    monkeypatch, capsys, tmp_path
) -> None:
    body = tmp_path / "prose.md"
    body.write_text("prose")
    monkeypatch.setattr(
        "sys.argv",
        [
            "github-checker",
            "issue-create",
            "/tmp/repo",
            "--slug",
            "wanted",
            "--title",
            "t",
            "--body-file",
            str(body),
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        main_module.main()
    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "--from" in payload["error"]


def test_issue_create_with_a_missing_body_file_returns_json(
    monkeypatch, capsys
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "github-checker",
            "issue-create",
            "/tmp/repo",
            "--slug",
            "wanted",
            "--from",
            "dispatcher",
            "--title",
            "t",
            "--body-file",
            "/no/such/file",
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        main_module.main()
    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    # Nothing was attempted — this must read as the known-negative `false`,
    # not the `_dispatch_guarded` catch-all's `null` ("may have landed").
    assert payload["created"] is False


def test_issue_create_with_a_non_utf8_body_file_reports_created_false(
    monkeypatch, capsys, tmp_path
) -> None:
    """A `--body-file` that fails to decode is a read failure before any
    mutation, so `created` must be the known-negative `false`. Before the
    fix this escaped `_run_issue_create`'s `except OSError` (UnicodeDecodeError
    is a ValueError, not an OSError), reached `_dispatch_guarded`'s
    catch-all, and reported `created: null` — a request that was never
    filed would then read as "may have landed", making it un-retriable.
    """
    body = tmp_path / "prose.bin"
    body.write_bytes(b"\xff\xfe\x00bad-utf8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "github-checker",
            "issue-create",
            "/tmp/repo",
            "--slug",
            "wanted",
            "--from",
            "dispatcher",
            "--title",
            "t",
            "--body-file",
            str(body),
        ],
    )
    with pytest.raises(SystemExit) as exit_info:
        main_module.main()
    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["created"] is False


def test_issue_lookup_prints_matches(monkeypatch, capsys) -> None:
    from github_checker.actions import result_for
    from github_checker.models import IssueRef

    ref = IssueRef(
        number=7,
        title="t",
        state="open",
        url="https://example.invalid/7",
        author="a",
        labels=["inbox"],
    )
    monkeypatch.setattr(
        "github_checker.issues.issue_lookup",
        lambda *a, **k: result_for(
            "issue-lookup", "/tmp/repo", ok=True, matches=[ref], malformed=[]
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["github-checker", "issue-lookup", "/tmp/repo", "--slug", "wanted"],
    )
    main_module.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["matches"][0]["number"] == 7


def test_body_file_is_decoded_as_utf8_not_the_locale(monkeypatch, tmp_path) -> None:
    """The non-UTF-8 refusal must behave identically on every machine.

    A locale-dependent decode cannot be observed on a UTF-8 host, so this
    pins the call site rather than the outcome: `read_text` must be given
    an explicit encoding.
    """
    seen: dict = {}
    real = Path.read_text

    def spy(self, *args, **kwargs):
        seen.update(kwargs)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", spy)
    body = tmp_path / "prose.md"
    body.write_text("prose", encoding="utf-8")
    monkeypatch.setattr(
        "github_checker.issues.issue_create",
        lambda *a, **k: __import__(
            "github_checker.actions", fromlist=["result_for"]
        ).result_for("issue-create", "/repo", ok=True, created=True),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "github-checker",
            "issue-create",
            "/tmp/repo",
            "--slug",
            "wanted",
            "--from",
            "dispatcher",
            "--title",
            "t",
            "--body-file",
            str(body),
        ],
    )
    main_module.main()
    assert seen.get("encoding") == "utf-8"


ARGV_REFUSALS = [
    # a flag value beginning with `-` — argparse reads it as another flag
    (["merge", "/tmp/repo", "7", "--if-head", "--limit"], "merge", "/tmp/repo"),
    # a verb this build does not have: a consumer probing for a newer one
    (["no-such-verb", "/tmp/repo"], "no-such-verb", "/tmp/repo"),
    # a required positional left out
    (["merge"], "merge", ""),
    # an extra positional
    (["pull", "/tmp/repo", "extra"], "pull", "/tmp/repo"),
    # no verb at all
    (["--nonsense"], "unknown", ""),
    (["issue-create", "/tmp/repo", "--slug", "--from"], "issue-create", "/tmp/repo"),
]


@pytest.mark.parametrize("argv, action, directory", ARGV_REFUSALS)
def test_argv_refusal_is_json_not_argparse_usage(
    monkeypatch, capsys, argv: list[str], action: str, directory: str
) -> None:
    """argparse's own error path writes usage to stderr and exits 2, printing
    no JSON at all — bypassing every handler's validation and breaking the
    contract machine consumers depend on."""
    monkeypatch.setattr("sys.argv", ["github-checker", *argv])
    with pytest.raises(SystemExit) as exit_info:
        main_module.main()
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["action"] == action
    assert payload["dir"] == directory
    assert "invalid command line" in payload["error"]


def test_help_still_exits_zero_without_json(monkeypatch, capsys) -> None:
    """`--help` reaches argparse's `exit()`, not `error()`. It is not a verb
    invocation, so it is outside the JSON contract and must stay that way."""
    monkeypatch.setattr("sys.argv", ["github-checker", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        main_module.main()
    assert exit_info.value.code == 0
    assert "usage:" in capsys.readouterr().out
