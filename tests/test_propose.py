"""propose-pr: scoped content-PR from an isolated worktree (spec 2026-07-17)."""

from pathlib import Path

import pytest

from github_checker.propose import (
    Edit,
    ProposeError,
    normalize_repo_path,
    parse_edits,
    parse_if_match,
)


def _content(tmp_path: Path, name: str = "c.txt", text: str = "new\n") -> Path:
    f = tmp_path / name
    f.write_text(text)
    return f


def test_parse_edits_splits_on_first_equals(tmp_path: Path) -> None:
    f = _content(tmp_path, "with=eq.txt")
    edits = parse_edits([f"docs/a.md={f}"])
    assert edits == [Edit(repo_path="docs/a.md", content_file=f)]


def test_parse_edits_rejects_missing_equals(tmp_path: Path) -> None:
    with pytest.raises(ProposeError, match="expected <repo-path>=<content-file>"):
        parse_edits(["no-separator"])


def test_parse_edits_rejects_unreadable_or_irregular_content(tmp_path: Path) -> None:
    with pytest.raises(ProposeError, match="not a readable regular file"):
        parse_edits([f"a.txt={tmp_path / 'absent'}"])
    d = tmp_path / "dir"
    d.mkdir()
    with pytest.raises(ProposeError, match="not a readable regular file"):
        parse_edits([f"a.txt={d}"])


def test_parse_edits_rejects_unreadable_permissions(tmp_path: Path) -> None:
    import os

    f = _content(tmp_path)
    f.chmod(0)
    try:
        if os.access(f, os.R_OK):  # e.g. running as root — cannot exercise this
            pytest.skip("filesystem grants read despite chmod 0")
        with pytest.raises(ProposeError, match="not a readable regular file"):
            parse_edits([f"a.txt={f}"])
    finally:
        f.chmod(0o600)


def test_parse_edits_duplicate_after_normalization(tmp_path: Path) -> None:
    f = _content(tmp_path)
    with pytest.raises(ProposeError, match="duplicate repo path"):
        parse_edits([f"a/b.txt={f}", f"./a//b.txt={f}"])


@pytest.mark.parametrize(
    "bad",
    [
        "/abs.txt",
        "../up.txt",
        "a/../b",  # normpath would collapse this to "b" — must reject RAW
        "a/../../up.txt",
        ".git/hooks/x",
        "a/.git/x",
        "",
    ],
)
def test_normalize_repo_path_rejects_escapes(bad: str) -> None:
    with pytest.raises(ProposeError):
        normalize_repo_path(bad)


def test_normalize_repo_path_normalizes() -> None:
    assert normalize_repo_path("./a//b.txt") == "a/b.txt"


def test_parse_if_match(tmp_path: Path) -> None:
    got = parse_if_match(["project.yaml=" + "a" * 64])
    assert got == {"project.yaml": "a" * 64}
    # uppercase hex is normalized, not rejected
    assert parse_if_match(["p.yaml=" + "A" * 64]) == {"p.yaml": "a" * 64}
    with pytest.raises(ProposeError, match="expected <repo-path>=<sha256>"):
        parse_if_match(["project.yaml"])
    with pytest.raises(ProposeError, match="not a sha256"):
        parse_if_match(["project.yaml=nothex"])
    with pytest.raises(ProposeError, match="duplicate repo path"):
        parse_if_match(["a/b=" + "a" * 64, "./a//b=" + "b" * 64])
