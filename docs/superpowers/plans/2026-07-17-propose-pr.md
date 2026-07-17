# propose-pr Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A new `github-checker propose-pr` command that applies caller-provided file content in a temporary worktree branched from `origin/<default>`, commits, pushes a fresh branch, and opens a PR — never reading or modifying the operator's live working-tree files.

**Architecture:** One new module `github_checker/propose.py` (parsing/validation + the `propose_pr()` action), three small helpers added to `github_checker/localgit.py` (default-branch resolution, raw blob bytes), four additive fields on `ActionResult` in `github_checker/actions.py`, CLI wiring in `github_checker/main.py`. `open-pr`/`pull` are not touched.

**Tech Stack:** Python 3.12, stdlib only (subprocess/tempfile/hashlib/secrets), pydantic v2 (existing), pytest with real temp git repos (existing `_make_pair` convention), `gh` faked via `monkeypatch.setattr(actions, "_gh", ...)`.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-17-propose-pr-design.md` — read it first; every §-reference below points there.
- Line length 88 (ruff), type hints required, `uv run pyrefly check` must pass after every task, `uv run ruff format --check .` must pass (CI runs format check — run `uv run ruff format .` before every commit).
- Git operations in tests are ALWAYS real (temp bare origin + clone); only `gh` is faked via `monkeypatch.setattr(actions, "_gh", ...)`. Never stub git.
- The operator's live working-tree **files** are never read as content source and never modified (spec §2 wording — shared `.git` metadata updates are expected and documented).
- Invariants: PR-only; the pushed branch is always freshly created from `origin/<default>`; never force-push; never touch `open-pr`/`pull` behavior.
- Every failure degrades to `ActionResult(ok=False, error=...)` — never an uncaught exception out of `propose_pr()`.

---

## File Structure

- Create: `github_checker/propose.py` — `ProposeError`, edit parsing/normalization, `propose_pr()`.
- Modify: `github_checker/localgit.py` — add `set_head_auto()`, `default_branch()`, `blob_bytes()`.
- Modify: `github_checker/actions.py` — 4 additive `ActionResult` fields (nothing else).
- Modify: `github_checker/main.py` — `propose-pr` subparser + dispatch.
- Modify: `README.md` — document the new command.
- Test: `tests/test_propose.py` (new), `tests/test_localgit.py` (extend). `tests/test_actions.py` is untouched.

---

### Task 1: Edit parsing & repo-path normalization (`propose.py`, pure functions)

**Files:**
- Create: `github_checker/propose.py`
- Test: `tests/test_propose.py`

**Interfaces:**
- Produces (used by Tasks 3-5):
  - `class ProposeError(Exception)` — message becomes `ActionResult.error`.
  - `@dataclass(frozen=True) class Edit: repo_path: str; content_file: Path` — `repo_path` normalized POSIX.
  - `parse_edits(raw: list[str]) -> list[Edit]` — raises `ProposeError`.
  - `parse_if_match(raw: list[str]) -> dict[str, str]` — repo-path → sha256 hex; raises `ProposeError`.
  - `normalize_repo_path(raw: str) -> str` — raises `ProposeError`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_propose.py
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


def test_parse_edits_duplicate_after_normalization(tmp_path: Path) -> None:
    f = _content(tmp_path)
    with pytest.raises(ProposeError, match="duplicate repo path"):
        parse_edits([f"a/b.txt={f}", f"./a//b.txt={f}"])


@pytest.mark.parametrize(
    "bad",
    ["/abs.txt", "../up.txt", "a/../../up.txt", ".git/hooks/x", "a/.git/x", ""],
)
def test_normalize_repo_path_rejects_escapes(bad: str) -> None:
    with pytest.raises(ProposeError):
        normalize_repo_path(bad)


def test_normalize_repo_path_normalizes() -> None:
    assert normalize_repo_path("./a//b.txt") == "a/b.txt"


def test_parse_if_match(tmp_path: Path) -> None:
    got = parse_if_match(["project.yaml=" + "a" * 64])
    assert got == {"project.yaml": "a" * 64}
    with pytest.raises(ProposeError, match="expected <repo-path>=<sha256>"):
        parse_if_match(["project.yaml"])
    with pytest.raises(ProposeError, match="not a sha256"):
        parse_if_match(["project.yaml=nothex"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_propose.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'github_checker.propose'`.

- [ ] **Step 3: Write the implementation**

```python
# github_checker/propose.py
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
    """Normalize a repo-relative POSIX path; reject anything that escapes."""
    if not raw or raw.startswith("/") or "\\" in raw:
        raise ProposeError(f"invalid repo path: {raw!r}")
    norm = posixpath.normpath(raw)
    parts = norm.split("/")
    if norm.startswith(("/", "../")) or ".." in parts or ".git" in parts:
        raise ProposeError(f"repo path escapes the repository: {raw!r}")
    if norm == ".":
        raise ProposeError(f"invalid repo path: {raw!r}")
    return norm


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
        edits.append(Edit(repo_path=repo_path, content_file=content_file))
    return edits


def parse_if_match(raw: list[str]) -> dict[str, str]:
    """Parse --if-match values into {normalized repo path: sha256 hex}."""
    guards: dict[str, str] = {}
    for item in raw:
        repo_raw, digest = _split_first_equals(item, "<repo-path>=<sha256>")
        if not _SHA256_RE.fullmatch(digest):
            raise ProposeError(f"not a sha256 hex digest: {digest!r}")
        guards[normalize_repo_path(repo_raw)] = digest
    return guards
```

- [ ] **Step 4: Run the tests, format, lint, type-check**

Run: `uv run pytest tests/test_propose.py -v && uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: 7 passed; format/lint/pyrefly clean.

- [ ] **Step 5: Commit**

```bash
git add github_checker/propose.py tests/test_propose.py
git commit -m "feat: propose-pr edit parsing and repo-path normalization (spec §1)"
```

---

### Task 2: localgit helpers — default branch + raw blob bytes

**Files:**
- Modify: `github_checker/localgit.py`
- Test: extend `tests/test_localgit.py`

**Interfaces:**
- Produces (used by Task 3):
  - `set_head_auto(path: Path) -> None` — best-effort `git remote set-head origin -a`; never raises.
  - `default_branch(path: Path) -> str | None` — parse `refs/remotes/origin/HEAD` via `git symbolic-ref`; None if unset.
  - `blob_bytes(path: Path, ref: str, repo_path: str) -> bytes | None` — raw bytes of `<ref>:<repo_path>` via `git cat-file blob` (no smudge filters); None if the path is absent at that ref.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_localgit.py` (read its imports first; it already has real-git fixtures — reuse the file's existing repo-building helpers if present, otherwise use this local pair builder, matching `tests/test_actions.py::_make_pair`):

```python
def _pair(tmp_path):
    import subprocess

    def g(path, *args):
        subprocess.run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
        )

    origin = tmp_path / "origin.git"
    origin.mkdir()
    g(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    g(seed, "init", "-q", "-b", "main")
    g(seed, "config", "user.email", "t@example.com")
    g(seed, "config", "user.name", "t")
    (seed / "f.txt").write_bytes(b"one\r\n")  # CRLF on purpose (raw-bytes test)
    g(seed, "add", "f.txt")
    g(seed, "commit", "-q", "-m", "init")
    g(seed, "remote", "add", "origin", str(origin))
    g(seed, "push", "-q", "-u", "origin", "main")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(clone)],
        check=True,
        capture_output=True,
    )
    return origin, seed, clone, g


def test_default_branch_from_fresh_clone(tmp_path):
    from github_checker.localgit import default_branch

    _, _, clone, _ = _pair(tmp_path)
    assert default_branch(clone) == "main"


def test_default_branch_none_when_head_unset(tmp_path):
    from github_checker.localgit import default_branch

    _, _, clone, g = _pair(tmp_path)
    g(clone, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    assert default_branch(clone) is None


def test_set_head_auto_refreshes_stale_head(tmp_path):
    from github_checker.localgit import default_branch, fetch, set_head_auto

    origin, seed, clone, g = _pair(tmp_path)
    # remote's default branch changes to new-main AFTER the clone
    g(seed, "switch", "-q", "-c", "new-main")
    g(seed, "push", "-q", "-u", "origin", "new-main")
    g(origin, "symbolic-ref", "HEAD", "refs/heads/new-main")
    assert default_branch(clone) == "main"  # stale
    fetch(clone)
    set_head_auto(clone)
    assert default_branch(clone) == "new-main"  # refreshed


def test_blob_bytes_raw_and_absent(tmp_path):
    from github_checker.localgit import blob_bytes

    _, _, clone, _ = _pair(tmp_path)
    assert blob_bytes(clone, "origin/main", "f.txt") == b"one\r\n"  # raw CRLF
    assert blob_bytes(clone, "origin/main", "missing.txt") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_localgit.py -v -k "default_branch or set_head or blob_bytes"`
Expected: FAIL — `ImportError` (names don't exist yet).

- [ ] **Step 3: Implement in `localgit.py`**

Append (reusing the module's existing `_git` helper and `LocalGitError`):

```python
def set_head_auto(path: Path) -> None:
    """Best-effort `git remote set-head origin -a`; never raises.

    origin/HEAD can be stale when the remote's default branch changed
    after clone — a plain fetch does not update it.
    """
    try:
        _git(path, "remote", "set-head", "origin", "-a")
    except LocalGitError:
        pass


def default_branch(path: Path) -> str | None:
    """Default branch per refs/remotes/origin/HEAD, or None if unset."""
    try:
        ref = _git(path, "symbolic-ref", "refs/remotes/origin/HEAD")
    except LocalGitError:
        return None
    prefix = "refs/remotes/origin/"
    return ref.removeprefix(prefix) if ref.startswith(prefix) else None


def blob_bytes(path: Path, ref: str, repo_path: str) -> bytes | None:
    """Raw bytes of `<ref>:<repo_path>` (no smudge filters); None if absent."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "cat-file", "blob", f"{ref}:{repo_path}"],
            capture_output=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as err:
        raise LocalGitError(str(err)) from err
    if result.returncode != 0:
        return None
    return result.stdout
```

- [ ] **Step 4: Run tests, format, lint, type-check**

Run: `uv run pytest tests/test_localgit.py -v && uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: all pass (new 4 + existing), clean.

- [ ] **Step 5: Commit**

```bash
git add github_checker/localgit.py tests/test_localgit.py
git commit -m "feat: localgit helpers for propose-pr — default branch, raw blob bytes (spec §1, §3)"
```

---

### Task 3: `ActionResult` fields + `propose_pr()` core flow

**Files:**
- Modify: `github_checker/actions.py` (4 additive fields ONLY)
- Modify: `github_checker/propose.py`
- Test: extend `tests/test_propose.py`

**Interfaces:**
- Consumes: Task 1's parsing, Task 2's helpers, `actions.ActionResult`, `actions._gh`, `localgit.fetch`.
- Produces (used by Tasks 4-5): `propose_pr(path: Path, *, message: str, edit_args: list[str], if_match_args: list[str] | None = None, branch: str | None = None) -> ActionResult`.

- [ ] **Step 1: Add the additive `ActionResult` fields**

In `github_checker/actions.py`, extend `ActionResult` (after `local`):

```python
    branch: str | None = None
    base_branch: str | None = None
    commit_sha: str | None = None
    changed_paths: list[str] | None = None
```

Run: `uv run pytest tests/test_actions.py -q` — Expected: all existing tests still pass (fields are additive with None defaults).

- [ ] **Step 2: Write the failing core-flow tests**

Append to `tests/test_propose.py` (reuse `_make_pair` semantics — copy the builder locally to keep the file self-contained, matching `tests/test_actions.py`; and the `_FakeProc` pattern):

```python
import json
import subprocess

from github_checker import actions
from github_checker.propose import propose_pr


def _git(path: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def _make_pair(tmp_path: Path) -> tuple[Path, Path]:
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
    return origin, clone


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _gh_ok(monkeypatch) -> None:
    monkeypatch.setattr(
        actions,
        "_gh",
        lambda path, *args: _FakeProc(0, stdout="https://github.com/o/r/pull/7\n"),
    )


def test_happy_path_lands_branch_in_origin_live_tree_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    origin, clone = _make_pair(tmp_path)
    _gh_ok(monkeypatch)
    live_before = (clone / "project.yaml").read_bytes()
    content = tmp_path / "new.yaml"
    content.write_text("spec_runner:\n  max_retries: 9\n")

    result = propose_pr(
        clone, message="bump retries", edit_args=[f"project.yaml={content}"]
    )

    assert result.ok, result.error
    assert result.pr_url == "https://github.com/o/r/pull/7"
    assert result.base_branch == "main"
    assert result.branch and result.branch.startswith("propose/")
    assert result.changed_paths == ["project.yaml"]
    assert result.commit_sha
    # the edit is on the remote branch
    blob = _git(origin, "show", f"{result.branch}:project.yaml")
    assert "max_retries: 9" in blob
    # the live working tree is byte-for-byte untouched
    assert (clone / "project.yaml").read_bytes() == live_before
    # temp worktree and local branch are cleaned up
    assert "propose/" not in _git(clone, "branch", "--list", "propose/*")
    assert result.branch not in _git(clone, "worktree", "list")


def test_dirty_live_checkout_same_file_still_bases_on_default(
    tmp_path: Path, monkeypatch
) -> None:
    origin, clone = _make_pair(tmp_path)
    _gh_ok(monkeypatch)
    (clone / "project.yaml").write_text("OPERATOR LOCAL WIP\n")
    content = tmp_path / "new.yaml"
    content.write_text("spec_runner:\n  max_retries: 9\n")

    result = propose_pr(
        clone, message="bump", edit_args=[f"project.yaml={content}"]
    )

    assert result.ok, result.error
    blob = _git(origin, "show", f"{result.branch}:project.yaml")
    assert "OPERATOR LOCAL WIP" not in blob  # based on origin/main, not live tree
    assert (clone / "project.yaml").read_text() == "OPERATOR LOCAL WIP\n"


def test_multiple_edits_one_commit(tmp_path: Path, monkeypatch) -> None:
    origin, clone = _make_pair(tmp_path)
    _gh_ok(monkeypatch)
    a = tmp_path / "a.txt"
    a.write_text("A\n")
    b = tmp_path / "b.txt"
    b.write_text("B\n")

    result = propose_pr(
        clone,
        message="two files",
        edit_args=[f"docs/a.txt={a}", f"b.txt={b}"],
    )

    assert result.ok, result.error
    assert result.changed_paths == ["b.txt", "docs/a.txt"]  # sorted
    count = _git(origin, "rev-list", "--count", f"main..{result.branch}")
    assert count == "1"


def test_noop_returns_structural_marker(tmp_path: Path, monkeypatch) -> None:
    _, clone = _make_pair(tmp_path)
    _gh_ok(monkeypatch)
    same = tmp_path / "same.yaml"
    same.write_text("spec_runner:\n  max_retries: 3\n")  # identical to origin/main

    result = propose_pr(
        clone, message="noop", edit_args=[f"project.yaml={same}"]
    )

    assert not result.ok
    assert result.detail == "no-op"
    assert result.error is not None
    # nothing was pushed
    assert result.branch is None or "propose" not in _git(
        clone, "ls-remote", "--heads", "origin", "propose/*"
    )


def test_parse_error_degrades_to_result(tmp_path: Path) -> None:
    _, clone = _make_pair(tmp_path)
    result = propose_pr(clone, message="x", edit_args=["no-separator"])
    assert not result.ok
    assert "expected" in (result.error or "")


def test_not_a_repo(tmp_path: Path) -> None:
    result = propose_pr(tmp_path, message="x", edit_args=[])
    assert not result.ok
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/test_propose.py -v -k "happy or dirty or multiple or noop or degrades or not_a_repo"`
Expected: FAIL — `ImportError: cannot import name 'propose_pr'`.

- [ ] **Step 4: Implement `propose_pr()` in `propose.py`**

Add imports at the top: `import hashlib`, `import secrets`, `import shutil`, `import subprocess`, `import tempfile`, `from datetime import UTC, datetime`, `from github_checker.actions import ActionResult, _gh`, `from github_checker.localgit import LocalGitError, _git, blob_bytes, default_branch, fetch, is_git_repo, set_head_auto`.

```python
def _fail(error: str, *, detail: str | None = None, **extra: object) -> ActionResult:
    return ActionResult(
        action="propose-pr", dir="", ok=False, error=error, detail=detail, **extra
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
        blob = blob_bytes(path, f"origin/{base}", repo_path)
        if blob is None or hashlib.sha256(blob).hexdigest() != expected:
            return _fail(
                "base file changed; reload required", detail=None
            ).model_copy(update={"dir": result_dir, "base_branch": base})

    head = branch or _generated_branch()
    try:
        _validate_branch(path, head, base)
    except ProposeError as err:
        return _fail(str(err)).model_copy(
            update={"dir": result_dir, "base_branch": base}
        )

    tmp = Path(tempfile.mkdtemp(prefix="propose-pr-"))
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
        )
        if diff.returncode == 0:
            return _fail(
                f"no changes vs {base}", detail="no-op"
            ).model_copy(update={"dir": result_dir, "base_branch": base})
        _git(worktree, "commit", "-m", message)
        commit_sha = _git(worktree, "rev-parse", "HEAD")
        _git(worktree, "push", "-u", "origin", head)
        pushed = True
        created = _gh(worktree, "pr", "create", "--fill")
        if created.returncode != 0:
            return _cleanup_remote_after_gh_failure(
                path,
                head,
                base,
                result_dir,
                created.stderr.strip() or "gh pr create failed",
            )
        url = (
            created.stdout.strip().splitlines()[-1]
            if created.stdout.strip()
            else None
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
    except (ProposeError, LocalGitError) as err:
        extra: dict[str, object] = {"dir": result_dir, "base_branch": base}
        if pushed:
            extra.update(_best_effort_delete_remote(path, head))
        return _fail(str(err)).model_copy(update=extra)
    finally:
        # tolerant of partial progress (spec §5): skip whatever never existed
        if worktree_created:
            subprocess.run(
                ["git", "-C", str(path), "worktree", "remove", "--force", str(worktree)],
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(path), "branch", "-D", head],
                capture_output=True,
            )
        shutil.rmtree(tmp, ignore_errors=True)


def _default_branch_fallback(path: Path) -> str | None:
    """`gh repo view` as the last network fallback (spec §3 step 3)."""
    proc = _gh(path, "repo", "view", "--json", "defaultBranchRef")
    if proc.returncode != 0:
        return None
    try:
        import json

        name = json.loads(proc.stdout)["defaultBranchRef"]["name"]
    except (ValueError, KeyError, TypeError):
        return None
    return str(name) if name else None


def _validate_branch(path: Path, head: str, base: str) -> None:
    """check-ref-format + refuse default/existing local/remote (spec §1)."""
    check = subprocess.run(
        ["git", "check-ref-format", "--branch", head], capture_output=True
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
        ["git", "-C", str(path), "show-ref", "--verify", f"refs/remotes/origin/{head}"],
        capture_output=True,
    )
    if local.returncode == 0 or remote.returncode == 0:
        raise ProposeError(f"branch already exists: {head}")


def _best_effort_delete_remote(path: Path, head: str) -> dict[str, object]:
    """Delete the pushed branch; on failure surface it via `branch` (spec §5)."""
    deleted = subprocess.run(
        ["git", "-C", str(path), "push", "origin", "--delete", head],
        capture_output=True,
    )
    return {} if deleted.returncode == 0 else {"branch": head}


def _cleanup_remote_after_gh_failure(
    path: Path, head: str, base: str, result_dir: str, error: str
) -> ActionResult:
    extra: dict[str, object] = {"dir": result_dir, "base_branch": base}
    extra.update(_best_effort_delete_remote(path, head))
    return _fail(error).model_copy(update=extra)
```

Note on `localgit._git`: it is module-private by convention but same-package reuse is intentional here (matching how `actions.py` composes localgit); if the implementer prefers, promote it to a public name in `localgit.py` instead — either is acceptable, do NOT duplicate the subprocess wrapper.

- [ ] **Step 5: Run all tests, format, lint, type-check**

Run: `uv run pytest -q && uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: full suite green (existing + new), clean.

- [ ] **Step 6: Commit**

```bash
git add github_checker/actions.py github_checker/propose.py tests/test_propose.py
git commit -m "feat: propose_pr core flow — worktree, no-op guard, cleanup (spec §2, §4-6)"
```

---

### Task 4: Guards — if-match, branch validation, symlink escape, gh-failure lifecycle

**Files:**
- Modify: `github_checker/propose.py` (only if a test exposes a gap — the Task 3 code already contains these guards; this task PROVES them)
- Test: extend `tests/test_propose.py`

**Interfaces:** none new — this task pins spec §8 cases 3-6, 9-12 with tests.

- [ ] **Step 1: Write the failing/pinning tests**

```python
import hashlib


def test_if_match_mismatch_no_branch_no_pr(tmp_path: Path, monkeypatch) -> None:
    origin, clone = _make_pair(tmp_path)
    _gh_ok(monkeypatch)
    content = tmp_path / "new.yaml"
    content.write_text("changed\n")
    stale_hash = hashlib.sha256(b"not what is on main").hexdigest()

    result = propose_pr(
        clone,
        message="stale",
        edit_args=[f"project.yaml={content}"],
        if_match_args=[f"project.yaml={stale_hash}"],
    )

    assert not result.ok
    assert "base file changed" in (result.error or "")
    assert _git(origin, "branch", "--list", "propose/*") == ""


def test_if_match_pass_with_raw_bytes(tmp_path: Path, monkeypatch) -> None:
    origin, clone = _make_pair(tmp_path)
    _gh_ok(monkeypatch)
    base_bytes = (clone / "project.yaml").read_bytes()
    content = tmp_path / "new.yaml"
    content.write_text("spec_runner:\n  max_retries: 9\n")

    result = propose_pr(
        clone,
        message="ok",
        edit_args=[f"project.yaml={content}"],
        if_match_args=[f"project.yaml={hashlib.sha256(base_bytes).hexdigest()}"],
    )
    assert result.ok, result.error


def test_custom_branch_existing_local_and_remote_refused(
    tmp_path: Path, monkeypatch
) -> None:
    origin, clone = _make_pair(tmp_path)
    _gh_ok(monkeypatch)
    content = tmp_path / "c.txt"
    content.write_text("x\n")
    _git(clone, "branch", "taken-local")
    _git(clone, "push", "-q", "origin", "main:taken-remote")
    _git(clone, "fetch", "-q")

    for bad in ("taken-local", "taken-remote", "main"):
        result = propose_pr(
            clone, message="x", edit_args=[f"n.txt={content}"], branch=bad
        )
        assert not result.ok, bad
        assert "exist" in (result.error or "") or "default" in (result.error or "")


def test_symlink_escape_refused_nothing_written_outside(
    tmp_path: Path, monkeypatch
) -> None:
    origin, clone = _make_pair(tmp_path)
    _gh_ok(monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    # a symlink dir committed on the default branch
    seed = tmp_path / "seed"
    (seed / "link").symlink_to(outside, target_is_directory=True)
    _git(seed, "add", "link")
    _git(seed, "commit", "-q", "-m", "add symlink")
    _git(seed, "push", "-q")
    content = tmp_path / "c.txt"
    content.write_text("escaped\n")

    result = propose_pr(
        clone, message="x", edit_args=[f"link/evil.txt={content}"]
    )

    assert not result.ok
    assert "symlink" in (result.error or "")
    assert list(outside.iterdir()) == []  # nothing escaped


def test_gh_failure_after_push_deletes_remote_branch(
    tmp_path: Path, monkeypatch
) -> None:
    origin, clone = _make_pair(tmp_path)
    monkeypatch.setattr(
        actions, "_gh", lambda path, *args: _FakeProc(1, stderr="gh exploded")
    )
    content = tmp_path / "c.txt"
    content.write_text("x\n")

    result = propose_pr(clone, message="x", edit_args=[f"n.txt={content}"])

    assert not result.ok
    assert "gh exploded" in (result.error or "")
    # the orphaned remote branch was cleaned up best-effort
    assert _git(origin, "branch", "--list", "propose/*") == ""
    # and since cleanup succeeded, branch is not surfaced
    assert result.branch is None


def test_stale_origin_head_resolves_new_default(
    tmp_path: Path, monkeypatch
) -> None:
    origin, clone = _make_pair(tmp_path)
    _gh_ok(monkeypatch)
    seed = tmp_path / "seed"
    _git(seed, "switch", "-q", "-c", "new-main")
    _git(seed, "push", "-q", "-u", "origin", "new-main")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/new-main")
    content = tmp_path / "c.txt"
    content.write_text("x\n")

    result = propose_pr(clone, message="x", edit_args=[f"n.txt={content}"])

    assert result.ok, result.error
    assert result.base_branch == "new-main"
```

- [ ] **Step 2: Run them**

Run: `uv run pytest tests/test_propose.py -v`
Expected: all pass IF Task 3's implementation is complete; any failure here is a real gap — fix `propose.py` minimally until green (the guards are already designed in, this task proves them against real git).

- [ ] **Step 3: Full suite, format, lint, type-check**

Run: `uv run pytest -q && uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: green and clean.

- [ ] **Step 4: Commit**

```bash
git add github_checker/propose.py tests/test_propose.py
git commit -m "test: pin propose-pr guards — if-match, branch refusal, symlink escape, gh-failure lifecycle (spec §8)"
```

---

### Task 5: CLI wiring + README

**Files:**
- Modify: `github_checker/main.py`
- Modify: `README.md`
- Test: extend `tests/test_main.py` (read its existing CLI-test convention first and match it)

**Interfaces:**
- Consumes: `propose.propose_pr` (Task 3).
- Produces: `github-checker propose-pr <dir> --message M --edit P=F [--edit ...] [--if-match P=SHA ...] [--branch B]` printing one JSON `ActionResult`, exit 1 when `ok=False` (same convention as `pull`/`open-pr`).

- [ ] **Step 1: Write the failing CLI test**

Read `tests/test_main.py` first to match how it invokes `main()` (argv monkeypatch/capsys). Add a test that runs `propose-pr` against a `_make_pair` clone with a monkeypatched `actions._gh` returning a PR URL, asserts exit code 0, and that stdout parses as JSON with `action == "propose-pr"`, `ok is True`, `pr_url`, `branch`, `base_branch`, `commit_sha`, `changed_paths`. Add a second test: missing `--edit` → exit 1, `ok is False` in the JSON. Follow the file's existing style exactly — do not invent a new harness.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_main.py -v -k propose`
Expected: FAIL — argparse errors with unknown command `propose-pr`.

- [ ] **Step 3: Wire the subparser in `main.py`**

Extend `_run_action` and the parser setup:

```python
def _run_propose(args: argparse.Namespace) -> None:
    """Run propose-pr and print its JSON result (exit 1 on failure)."""
    from github_checker.propose import propose_pr

    result = propose_pr(
        args.dir,
        message=args.message,
        edit_args=args.edit,
        if_match_args=args.if_match,
        branch=args.branch,
    )
    print(result.model_dump_json(indent=2))
    if not result.ok:
        raise SystemExit(1)
```

In `main()`, after the existing pull/open-pr loop:

```python
    prop = sub.add_parser(
        "propose-pr",
        help=(
            "apply explicit file content in a temp worktree off the default "
            "branch, push a fresh branch, open a PR; prints a JSON result"
        ),
    )
    prop.add_argument("dir", type=Path, help="path to the local clone")
    prop.add_argument("--message", required=True, help="commit message (PR title)")
    prop.add_argument(
        "--edit",
        action="append",
        required=True,
        metavar="REPO_PATH=CONTENT_FILE",
        help="file to create/replace (repeatable)",
    )
    prop.add_argument(
        "--if-match",
        action="append",
        default=[],
        dest="if_match",
        metavar="REPO_PATH=SHA256",
        help="stale-base guard: sha256 of the base content the caller saw",
    )
    prop.add_argument("--branch", default=None, help="head branch name (generated if omitted)")
```

And in the dispatch chain: `elif args.command == "propose-pr": _run_propose(args)`.

- [ ] **Step 4: Update `README.md`**

Add a `propose-pr` subsection next to the existing headless-actions documentation: one usage example (dispatcher's case: `--edit project.yaml=/tmp/rendered.yaml --if-match project.yaml=<sha256>`), the no-op `detail="no-op"` marker, the invariants line (fresh branch off default, never force, never default-branch push, live working-tree files untouched).

- [ ] **Step 5: Full suite, format, lint, type-check**

Run: `uv run pytest -q && uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: green and clean.

- [ ] **Step 6: Commit**

```bash
git add github_checker/main.py README.md tests/test_main.py
git commit -m "feat: propose-pr CLI wiring + README (spec §1)"
```

---

## Self-Review Notes

- **Spec coverage:** §1 contract/validation → Tasks 1, 3, 4; §2 mechanics → Task 3; §3 default-branch chain → Tasks 2-3 (`set_head_auto` → `default_branch` → `_default_branch_fallback` → error); §4 no-op `detail="no-op"` → Task 3; §5 lifecycle/cleanup-tolerance → Tasks 3-4; §6 ActionResult fields → Task 3; §7 invariants → enforced by construction (fresh branch, no force, only `push -u origin <head>` ever pushes); §8 test cases 1-12 → happy(3), dirty(3), if-match(4), branch-exists(4), symlink(4), gh-fail(4), multi-edit(3), duplicate(1), HEAD-absent(2), HEAD-stale(2+4), no-op(3), .git-component(1 parametrized); §9 out-of-scope respected (no deletions, no dirty-paths mode, open-pr untouched).
- **Placeholder scan:** clean — every step has runnable code or an exact command; Task 5 Step 1 delegates test-harness style to the existing file's convention deliberately (reading it is the step), with the exact assertions enumerated.
- **Type consistency:** `propose_pr` keyword signature matches Task 5's call site; `Edit`/`parse_edits`/`parse_if_match` names match between Tasks 1 and 3; localgit helper names match between Tasks 2 and 3.
- **Known judgment call recorded:** Task 3 reuses `localgit._git` (module-private, same package) rather than duplicating the subprocess wrapper — flagged in-plan with an explicit alternative (promote to public name) so the implementer/reviewer can choose without a round-trip.
