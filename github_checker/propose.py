"""propose-pr: apply explicit file content in an isolated worktree, open a PR.

A qualitatively different privilege level from `open-pr` (which never
pushes): this command commits and pushes — but only a freshly created
branch off origin/<default>, never the default branch itself, and never
with force. The operator's live working-tree files are never read as
content source and never modified; shared .git refs/objects are updated
(fetch, worktree, temporary local branch), same class of metadata effect
as `pull`. Spec: docs/superpowers/specs/2026-07-17-propose-pr-design.md.
"""

import posixpath
import re
from dataclasses import dataclass
from pathlib import Path

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class ProposeError(Exception):
    """Invalid propose-pr input; the message becomes ActionResult.error."""


@dataclass(frozen=True)
class Edit:
    """One --edit: normalized repo-relative POSIX path plus its content file."""

    repo_path: str
    content_file: Path


def normalize_repo_path(raw: str) -> str:
    """Normalize a repo-relative POSIX path; reject anything that escapes.

    `..`/`.git` are checked on the RAW parts, BEFORE normalization —
    normpath would silently collapse `a/../b` into `b` and hide the escape.
    """
    if not raw or raw.startswith("/") or "\\" in raw:
        raise ProposeError(f"invalid repo path: {raw!r}")
    raw_parts = [p for p in raw.split("/") if p not in ("", ".")]
    if ".." in raw_parts or ".git" in raw_parts:
        raise ProposeError(f"repo path escapes the repository: {raw!r}")
    if not raw_parts:
        raise ProposeError(f"invalid repo path: {raw!r}")
    return posixpath.normpath(raw)


def _split_first_equals(raw: str, what: str) -> tuple[str, str]:
    left, sep, right = raw.partition("=")
    if not sep or not left or not right:
        raise ProposeError(f"expected {what}, got: {raw!r}")
    return left, right


def parse_edits(raw: list[str]) -> list[Edit]:
    """Parse --edit values; duplicates checked after normalization."""
    edits: list[Edit] = []
    seen: set[str] = set()
    for item in raw:
        repo_raw, file_raw = _split_first_equals(item, "<repo-path>=<content-file>")
        repo_path = normalize_repo_path(repo_raw)
        if repo_path in seen:
            raise ProposeError(f"duplicate repo path: {repo_path}")
        seen.add(repo_path)
        content_file = Path(file_raw)
        if not content_file.is_file():
            raise ProposeError(f"not a readable regular file: {content_file}")
        try:
            with content_file.open("rb"):
                pass  # readability, not just existence, is checked at parse time
        except OSError as err:
            raise ProposeError(f"not a readable regular file: {content_file}") from err
        edits.append(Edit(repo_path=repo_path, content_file=content_file))
    return edits


def parse_if_match(raw: list[str]) -> dict[str, str]:
    """Parse --if-match values into {normalized repo path: sha256 hex}."""
    guards: dict[str, str] = {}
    for item in raw:
        repo_raw, digest = _split_first_equals(item, "<repo-path>=<sha256>")
        digest = digest.lower()
        if not _SHA256_RE.fullmatch(digest):
            raise ProposeError(f"not a sha256 hex digest: {digest!r}")
        repo_path = normalize_repo_path(repo_raw)
        if repo_path in guards:
            raise ProposeError(f"duplicate repo path: {repo_path}")
        guards[repo_path] = digest
    return guards
