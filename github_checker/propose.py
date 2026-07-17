"""propose-pr: apply explicit file content in an isolated worktree, open a PR.

A qualitatively different privilege level from `open-pr` (which never
pushes): this command commits and pushes — but only a freshly created
branch off origin/<default>, never the default branch itself, and never
with force. The operator's live working-tree files are never read as
content source and never modified; shared .git refs/objects are updated
(fetch, worktree, temporary local branch), same class of metadata effect
as `pull`. Spec: docs/superpowers/specs/2026-07-17-propose-pr-design.md.
"""

import hashlib
import json
import posixpath
import re
import secrets
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from github_checker import actions
from github_checker.actions import ActionResult
from github_checker.localgit import (
    LocalGitError,
    _git,
    blob_bytes,
    default_branch,
    fetch,
    is_git_repo,
    set_head_auto,
)

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
    if ".." in raw_parts or any(p.lower() == ".git" for p in raw_parts):
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


def _fail(error: str, *, detail: str | None = None) -> ActionResult:
    return ActionResult(
        action="propose-pr", dir="", ok=False, error=error, detail=detail
    )


def _generated_branch() -> str:
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    return f"propose/{stamp}-{secrets.token_hex(3)}"


def _apply_edit(worktree: Path, edit: Edit) -> None:
    """Write one edit inside the worktree; symlink-escape defense (spec §1)."""
    target = worktree / edit.repo_path
    probe = worktree
    for part in Path(edit.repo_path).parts:
        probe = probe / part
        if probe.is_symlink():
            raise ProposeError(f"symlink in repo path: {edit.repo_path}")
    resolved = target.parent.resolve() / target.name
    if not resolved.is_relative_to(worktree.resolve()):
        raise ProposeError(f"repo path escapes the worktree: {edit.repo_path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(edit.content_file.read_bytes())


def propose_pr(
    path: Path,
    *,
    message: str,
    edit_args: list[str],
    if_match_args: list[str] | None = None,
    branch: str | None = None,
) -> ActionResult:
    """Apply edits in a temp worktree off origin/<default>, push, open a PR."""
    result_dir = str(path)
    if not is_git_repo(path):
        return _fail("not a git repository").model_copy(update={"dir": result_dir})
    try:
        edits = parse_edits(edit_args)
        guards = parse_if_match(if_match_args or [])
        if not edits:
            raise ProposeError("at least one --edit is required")
    except ProposeError as err:
        return _fail(str(err)).model_copy(update={"dir": result_dir})

    try:
        fetch(path)
    except LocalGitError as err:
        return _fail(f"fetch failed: {err}").model_copy(update={"dir": result_dir})
    set_head_auto(path)
    base = default_branch(path) or _default_branch_fallback(path)
    if base is None:
        return _fail("cannot determine default branch").model_copy(
            update={"dir": result_dir}
        )

    # stale-base guard: raw blob bytes vs the hash the caller saw (spec §1)
    for repo_path, expected in guards.items():
        try:
            blob = blob_bytes(path, f"origin/{base}", repo_path)
        except LocalGitError as err:
            return _fail(str(err)).model_copy(
                update={"dir": result_dir, "base_branch": base}
            )
        if blob is None or hashlib.sha256(blob).hexdigest() != expected:
            return _fail("base file changed; reload required", detail=None).model_copy(
                update={"dir": result_dir, "base_branch": base}
            )

    head = branch or _generated_branch()
    try:
        _validate_branch(path, head, base)
    except ProposeError as err:
        return _fail(str(err)).model_copy(
            update={"dir": result_dir, "base_branch": base}
        )

    try:
        tmp = Path(tempfile.mkdtemp(prefix="propose-pr-"))
    except OSError as err:
        return _fail(f"cannot create temp dir: {err}").model_copy(
            update={"dir": result_dir, "base_branch": base}
        )
    worktree = tmp / "wt"
    worktree_created = False
    pushed = False
    try:
        _git(path, "worktree", "add", str(worktree), "-b", head, f"origin/{base}")
        worktree_created = True
        for edit in edits:
            _apply_edit(worktree, edit)
        changed = sorted(e.repo_path for e in edits)
        _git(worktree, "add", "--", *changed)
        diff = subprocess.run(
            ["git", "-C", str(worktree), "diff", "--cached", "--quiet"],
            capture_output=True,
            text=True,
        )
        if diff.returncode == 0:
            return _fail(f"no changes vs {base}", detail="no-op").model_copy(
                update={"dir": result_dir, "base_branch": base}
            )
        if diff.returncode != 1:
            # --quiet contract: 0 = clean, 1 = changes; anything else is a
            # real git error — do NOT proceed to commit/push on it
            raise LocalGitError(diff.stderr.strip() or "git diff --cached failed")
        _git(worktree, "commit", "-m", message)
        commit_sha = _git(worktree, "rev-parse", "HEAD")
        _git(worktree, "push", "-u", "origin", head)
        pushed = True
        created = actions._gh(worktree, "pr", "create", "--fill")
        if created.returncode != 0:
            return _cleanup_remote_after_gh_failure(
                path,
                head,
                base,
                result_dir,
                created.stderr.strip() or "gh pr create failed",
            )
        url = (
            created.stdout.strip().splitlines()[-1] if created.stdout.strip() else None
        )
        if not url:
            return _cleanup_remote_after_gh_failure(
                path,
                head,
                base,
                result_dir,
                "`gh pr create` succeeded but returned no PR URL",
            )
        return ActionResult(
            action="propose-pr",
            dir=result_dir,
            ok=True,
            detail="pull request created",
            pr_url=url,
            pr_state="OPEN",
            branch=head,
            base_branch=base,
            commit_sha=commit_sha,
            changed_paths=changed,
        )
    except (ProposeError, LocalGitError, OSError) as err:
        # OSError included: content_file may vanish between parse and apply
        # (TOCTOU), worktree writes may hit permissions — the global
        # constraint promises degradation to a result, not a traceback.
        extra: dict[str, object] = {"dir": result_dir, "base_branch": base}
        if pushed:
            extra.update(_best_effort_delete_remote(path, head))
        return _fail(str(err)).model_copy(update=extra)
    finally:
        # tolerant of partial progress (spec §5): skip whatever never existed
        if worktree_created:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(path),
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree),
                ],
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(path), "branch", "-D", head],
                capture_output=True,
            )
        shutil.rmtree(tmp, ignore_errors=True)


def _default_branch_fallback(path: Path) -> str | None:
    """Network fallbacks (spec §3 step 3): `remote show`, then `gh repo view`.

    `git remote show origin` comes first so the feature works even when
    `gh` is missing or unauthenticated.
    """
    try:
        shown = _git(path, "remote", "show", "origin")
    except LocalGitError:
        shown = ""
    for line in shown.splitlines():
        stripped = line.strip()
        if stripped.startswith("HEAD branch:"):
            name = stripped.removeprefix("HEAD branch:").strip()
            if name and name != "(unknown)":
                return name
    proc = actions._gh(path, "repo", "view", "--json", "defaultBranchRef")
    if proc.returncode != 0:
        return None
    try:
        name = json.loads(proc.stdout)["defaultBranchRef"]["name"]
    except (ValueError, KeyError, TypeError):
        return None
    return str(name) if name else None


def _validate_branch(path: Path, head: str, base: str) -> None:
    """check-ref-format + refuse default/existing local/remote (spec §1).

    `-C <path>` matters: `--branch` expands `@{-N}` syntax and some git
    versions refuse it outside a repository — validate in the target repo.
    """
    check = subprocess.run(
        ["git", "-C", str(path), "check-ref-format", "--branch", head],
        capture_output=True,
    )
    if check.returncode != 0:
        raise ProposeError(f"invalid branch name: {head!r}")
    if head == base:
        raise ProposeError(f"refusing to target the default branch: {head}")
    local = subprocess.run(
        ["git", "-C", str(path), "show-ref", "--verify", f"refs/heads/{head}"],
        capture_output=True,
    )
    remote = subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "show-ref",
            "--verify",
            f"refs/remotes/origin/{head}",
        ],
        capture_output=True,
    )
    if local.returncode == 0 or remote.returncode == 0:
        raise ProposeError(f"branch already exists: {head}")


def _best_effort_delete_remote(path: Path, head: str) -> dict[str, object]:
    """Delete the pushed branch; on failure surface it via `branch` (spec §5).

    Goes through `_git` (30s timeout) — this is a networked push and a raw
    subprocess.run could hang forever on a credential prompt or a stalled
    transport. Any failure, including timeout, is a failed cleanup.
    """
    try:
        _git(path, "push", "origin", "--delete", head)
    except LocalGitError:
        return {"branch": head}
    return {}


def _cleanup_remote_after_gh_failure(
    path: Path, head: str, base: str, result_dir: str, error: str
) -> ActionResult:
    extra: dict[str, object] = {"dir": result_dir, "base_branch": base}
    extra.update(_best_effort_delete_remote(path, head))
    return _fail(error).model_copy(update=extra)
