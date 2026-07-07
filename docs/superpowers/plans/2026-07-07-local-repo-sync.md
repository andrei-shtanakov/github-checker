# Local repo link & sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In the TUI details pane, show a clickable GitHub link per repo, an optional local clone path, its desync vs upstream (ahead/behind/dirty), and hotkeys to safely sync a clone.

**Architecture:** A new `localgit.py` module isolates all `git` subprocess work (synchronous, called via `asyncio.to_thread`), knowing nothing about Textual or the GitHub API. Config repos become `RepoRef` tables with an optional `path`; local status flows through the existing config → fetch → `RepoState` → details-pane pipeline.

**Tech Stack:** Python 3, pydantic v2, Textual, Rich (`rich.text.Text`), `git` CLI, pytest + anyio.

## Global Constraints

- Package manager: `uv` only (`uv run pytest`, `uv add`). Never pip.
- Type hints on all code; run `uv run pyrefly check` and fix before commit.
- Format/lint: `uv run ruff format .` then `uv run ruff check . --fix`.
- Line length: 88 chars max.
- Public functions get docstrings; f-strings for formatting; snake_case.
- Repo identifiers are `owner/repo`, validated by `REPO_RE`.
- `git` operations must never raise into the UI thread — `local_status` returns
  an error field; `fetch`/`pull` raise `LocalGitError` caught by callers.
- New features require tests; changed behavior requires updated tests.

---

## File Structure

- `github_checker/models.py` — add `RepoRef`, `LocalStatus`; change `Config.repos`
  to `list[RepoRef]` with a before-validator; add `path`/`local` to `RepoState`.
- `github_checker/localgit.py` — NEW. `LocalGitError`, `local_status`, `fetch`,
  `pull_ff_only`.
- `github_checker/config.py` — `save_config` serialization; `add_repo`/`remove_repo`
  work on `RepoRef`.
- `github_checker/github.py` — `fetch_all`/`fetch_repo` accept `RepoRef | str`,
  attach local status to `RepoState`.
- `github_checker/app.py` — details render to `rich.text.Text` with clickable
  link + local block; `s`/`S` sync actions; fix `repos` comparisons.
- `repos.toml.example` — show remote-only and path entries.
- `tests/test_models.py`, `tests/test_config.py`, `tests/test_fetch.py`,
  `tests/test_app.py` — updated; `tests/test_localgit.py` — NEW.

---

### Task 1: Models — `RepoRef`, `LocalStatus`, `Config` coercion, `RepoState` fields

**Files:**
- Modify: `github_checker/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces:
  - `class RepoRef(BaseModel)` with `name: str` (validated by `REPO_RE`),
    `path: Path | None = None`.
  - `class Config(BaseModel)` with `repos: list[RepoRef] = []`
    (before-validator coerces `str` items to `{"name": str}`),
    `refresh_seconds: int = 120`.
  - `class LocalStatus(BaseModel)` with `branch: str | None`,
    `ahead: int | None`, `behind: int | None`, `dirty: bool`,
    `error: str | None`.
  - `RepoState` gains `path: Path | None = None`, `local: LocalStatus | None = None`.

- [ ] **Step 1: Write the failing tests**

Replace the two `Config` tests and add coercion/field tests in `tests/test_models.py`:

```python
from pathlib import Path

import pytest
from pydantic import ValidationError

from github_checker.models import (
    Config,
    LocalStatus,
    PullRequest,
    RepoRef,
    RepoState,
)


def test_config_coerces_string_repos() -> None:
    config = Config(repos=["owner/repo"])
    assert config.repos == [RepoRef(name="owner/repo")]
    assert config.repos[0].path is None
    assert config.refresh_seconds == 120


def test_config_accepts_repo_ref_with_path() -> None:
    config = Config(repos=[{"name": "owner/repo", "path": "/tmp/repo"}])
    assert config.repos[0].path == Path("/tmp/repo")


def test_config_rejects_bad_repo() -> None:
    with pytest.raises(ValidationError):
        Config(repos=["not-a-repo"])


def test_repo_ref_rejects_bad_name() -> None:
    with pytest.raises(ValidationError):
        RepoRef(name="garbage")


def test_repo_state_defaults() -> None:
    state = RepoState(name="o/r")
    assert state.pulls == []
    assert state.alerts is None
    assert state.error is None
    assert state.path is None
    assert state.local is None


def test_local_status_holds_desync() -> None:
    status = LocalStatus(
        branch="main", ahead=2, behind=1, dirty=True, error=None
    )
    assert status.ahead == 2
    assert status.dirty is True


def test_pull_request_optional_copilot() -> None:
    pr = PullRequest(
        number=1, title="t", author="a", head_branch="b", is_dependabot=False
    )
    assert pr.copilot_review is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL — `ImportError` on `RepoRef`/`LocalStatus`.

- [ ] **Step 3: Implement the models**

Edit `github_checker/models.py`. Add `from pathlib import Path` to imports and
`field_validator` is already imported. Replace the `Config` class and add the
new classes:

```python
class RepoRef(BaseModel):
    """A tracked repository: owner/repo plus an optional local clone path."""

    name: str
    path: Path | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not REPO_RE.match(value):
            raise ValueError(f"invalid repo (expected owner/repo): {value!r}")
        return value


class Config(BaseModel):
    """Application configuration stored in repos.toml."""

    repos: list[RepoRef] = []
    refresh_seconds: int = 120

    @field_validator("repos", mode="before")
    @classmethod
    def _coerce_repos(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        return [{"name": item} if isinstance(item, str) else item for item in value]
```

Delete the old `Config` class (the one with `repos: list[str]` and its
`_validate_repos` validator) — `REPO_RE` now lives on `RepoRef`.

Add `LocalStatus` (place it above `RepoState`):

```python
class LocalStatus(BaseModel):
    """State of a local clone relative to its upstream."""

    branch: str | None
    ahead: int | None
    behind: int | None
    dirty: bool
    error: str | None = None
```

Add the two fields to `RepoState`:

```python
    path: Path | None = None
    local: LocalStatus | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: PASS (all).

- [ ] **Step 5: Type-check, format, commit**

```bash
uv run ruff format github_checker/models.py tests/test_models.py
uv run ruff check github_checker/models.py tests/test_models.py --fix
uv run pyrefly check
git add github_checker/models.py tests/test_models.py
git commit -m "feat: RepoRef with optional path, LocalStatus model"
```

---

### Task 2: Config persistence for the new schema

**Files:**
- Modify: `github_checker/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `RepoRef`, `Config` from Task 1.
- Produces:
  - `save_config(path, config)` — writes valid TOML with `refresh_seconds`
    before `[[repos]]` tables; omits `path` when `None`.
  - `add_repo(path, name) -> Config` — appends `RepoRef(name=name)`.
  - `remove_repo(path, name) -> Config` — drops refs whose `name == name`.

- [ ] **Step 1: Write the failing tests**

Update `tests/test_config.py`. Change the top import to add `RepoRef`:

```python
from github_checker.models import Config, RepoRef
```

Update every assertion that compares `.repos` to a list of strings so it compares
names, and add round-trip coverage for paths. Concretely, edit these tests:

```python
def test_resolve_config_path_migrates_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    workdir = tmp_path / "project"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    save_config(workdir / "repos.toml", Config(repos=["o/legacy"]))
    resolved = resolve_config_path(None)
    assert resolved == tmp_path / "xdg" / "github-checker" / "repos.toml"
    assert [r.name for r in load_config(resolved).repos] == ["o/legacy"]
    assert not (workdir / "repos.toml").exists()


def test_resolve_config_path_existing_target_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    workdir = tmp_path / "project"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    target = tmp_path / "xdg" / "github-checker" / "repos.toml"
    save_config(target, Config(repos=["o/mine"]))
    save_config(workdir / "repos.toml", Config(repos=["o/legacy"]))
    resolved = resolve_config_path(None)
    assert [r.name for r in load_config(resolved).repos] == ["o/mine"]


def test_save_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=["o/r"], refresh_seconds=30))
    loaded = load_config(path)
    assert [r.name for r in loaded.repos] == ["o/r"]
    assert loaded.repos[0].path is None
    assert loaded.refresh_seconds == 30


def test_save_load_roundtrip_with_path(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    ref = RepoRef(name="o/r", path=Path("/tmp/o-r"))
    save_config(path, Config(repos=[ref]))
    loaded = load_config(path)
    assert loaded.repos == [ref]


def test_add_repo(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=["o/r"]))
    config = add_repo(path, "o/two")
    assert [r.name for r in config.repos] == ["o/r", "o/two"]
    assert [r.name for r in load_config(path).repos] == ["o/r", "o/two"]


def test_add_repo_preserves_existing_path(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=[RepoRef(name="o/r", path=Path("/tmp/o-r"))]))
    config = add_repo(path, "o/two")
    assert config.repos[0].path == Path("/tmp/o-r")


def test_add_repo_duplicate_noop(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=["o/r"]))
    assert [r.name for r in add_repo(path, "o/r").repos] == ["o/r"]


def test_remove_repo(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=["o/r", "o/two"]))
    config = remove_repo(path, "o/r")
    assert [r.name for r in config.repos] == ["o/two"]
    assert [r.name for r in load_config(path).repos] == ["o/two"]
```

Leave `test_add_repo_invalid_raises`, `test_load_missing_creates_empty`,
`test_load_missing_creates_parent_dirs`, and the two path-resolution tests that
don't touch `.repos` as they are.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `save_config` writes `None` for `path` (TOML cannot serialize
`None`) and/or `add_repo`/`remove_repo` build `RepoRef` wrong.

- [ ] **Step 3: Update config.py**

In `github_checker/config.py` change `save_config` serialization and rebuild
`add_repo`/`remove_repo`:

```python
def save_config(path: Path, config: Config) -> None:
    """Write *config* to *path* as TOML, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = config.model_dump(mode="json", exclude_none=True)
    path.write_text(tomli_w.dumps(data), encoding="utf-8")


def add_repo(path: Path, name: str) -> Config:
    """Add *name* to the config file; duplicates are ignored."""
    config = load_config(path)
    if any(ref.name == name for ref in config.repos):
        return config
    updated = Config(
        repos=[*config.repos, RepoRef(name=name)],
        refresh_seconds=config.refresh_seconds,
    )
    save_config(path, updated)
    return updated


def remove_repo(path: Path, name: str) -> Config:
    """Remove *name* from the config file if present."""
    config = load_config(path)
    updated = config.model_copy(
        update={"repos": [ref for ref in config.repos if ref.name != name]}
    )
    save_config(path, updated)
    return updated
```

Add `RepoRef` to the models import at the top:

```python
from github_checker.models import Config, RepoRef
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (all). If `refresh_seconds` lands under a `[[repos]]` table,
`test_save_load_roundtrip` will fail on load — confirm `tomli_w` emitted
`refresh_seconds` before the first `[[repos]]` (it does by placing scalars first).

- [ ] **Step 5: Type-check, format, commit**

```bash
uv run ruff format github_checker/config.py tests/test_config.py
uv run ruff check github_checker/config.py tests/test_config.py --fix
uv run pyrefly check
git add github_checker/config.py tests/test_config.py
git commit -m "feat: persist RepoRef entries with optional path"
```

---

### Task 3: `localgit.py` — status and safe sync

**Files:**
- Create: `github_checker/localgit.py`
- Test: `tests/test_localgit.py`

**Interfaces:**
- Consumes: `LocalStatus` from Task 1.
- Produces:
  - `class LocalGitError(Exception)`.
  - `local_status(path: Path) -> LocalStatus` — never raises.
  - `fetch(path: Path) -> None` — raises `LocalGitError` on failure.
  - `pull_ff_only(path: Path) -> None` — raises `LocalGitError` on failure.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_localgit.py`:

```python
import subprocess
from pathlib import Path

import pytest

from github_checker.localgit import (
    LocalGitError,
    fetch,
    local_status,
    pull_ff_only,
)


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")
    (path / "f.txt").write_text("one\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "init")


def test_local_status_missing_path(tmp_path: Path) -> None:
    status = local_status(tmp_path / "nope")
    assert status.error is not None
    assert status.branch is None


def test_local_status_non_git_dir(tmp_path: Path) -> None:
    status = local_status(tmp_path)
    assert status.error is not None


def test_local_status_no_upstream(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    status = local_status(repo)
    assert status.error is None
    assert status.branch == "main"
    assert status.ahead is None
    assert status.behind is None
    assert status.dirty is False


def test_local_status_dirty(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "untracked.txt").write_text("x\n")
    assert local_status(repo).dirty is True


def test_local_status_ahead_of_upstream(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare")
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    (repo / "f.txt").write_text("two\n")
    _git(repo, "commit", "-q", "-am", "second")
    status = local_status(repo)
    assert status.ahead == 1
    assert status.behind == 0


def test_fetch_without_remote_raises(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    with pytest.raises(LocalGitError):
        fetch(repo)


def test_pull_ff_only_succeeds(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare")
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    fetch(repo)          # no error now that a remote exists
    pull_ff_only(repo)   # already up to date -> ff-only is a no-op, no error
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_localgit.py -v`
Expected: FAIL — `ModuleNotFoundError: github_checker.localgit`.

- [ ] **Step 3: Implement localgit.py**

Create `github_checker/localgit.py`:

```python
"""Local git clone status and safe sync operations."""

import subprocess
from pathlib import Path

from github_checker.models import LocalStatus


class LocalGitError(Exception):
    """A failed local git operation."""


def _git(path: Path, *args: str) -> str:
    """Run `git -C path *args`, returning stripped stdout or raising."""
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise LocalGitError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


def local_status(path: Path) -> LocalStatus:
    """Describe a clone relative to its upstream; never raises."""
    if not path.exists():
        return LocalStatus(
            branch=None, ahead=None, behind=None, dirty=False,
            error="путь не найден",
        )
    try:
        branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
        dirty = bool(_git(path, "status", "--porcelain"))
        ahead: int | None = None
        behind: int | None = None
        try:
            counts = _git(
                path, "rev-list", "--left-right", "--count", "@{upstream}...HEAD"
            )
            behind_str, ahead_str = counts.split()
            behind, ahead = int(behind_str), int(ahead_str)
        except LocalGitError:
            pass  # no upstream configured
        return LocalStatus(
            branch=branch, ahead=ahead, behind=behind, dirty=dirty, error=None
        )
    except LocalGitError as err:
        return LocalStatus(
            branch=None, ahead=None, behind=None, dirty=False, error=str(err)
        )


def fetch(path: Path) -> None:
    """Run `git fetch --prune`; raises LocalGitError on failure."""
    _git(path, "fetch", "--prune")


def pull_ff_only(path: Path) -> None:
    """Run `git pull --ff-only`; raises LocalGitError on divergence/failure."""
    _git(path, "pull", "--ff-only")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_localgit.py -v`
Expected: PASS (all).

- [ ] **Step 5: Type-check, format, commit**

```bash
uv run ruff format github_checker/localgit.py tests/test_localgit.py
uv run ruff check github_checker/localgit.py tests/test_localgit.py --fix
uv run pyrefly check
git add github_checker/localgit.py tests/test_localgit.py
git commit -m "feat: localgit status + fetch/pull-ff-only"
```

---

### Task 4: Attach local status in the fetch pipeline

**Files:**
- Modify: `github_checker/github.py`
- Test: `tests/test_fetch.py`

**Interfaces:**
- Consumes: `RepoRef` (Task 1), `local_status` (Task 3).
- Produces:
  - `fetch_all(repos: list[RepoRef | str]) -> list[RepoState]` — coerces `str`
    items to `RepoRef(name=...)`.
  - `fetch_repo(ref: RepoRef, sem) -> RepoState` — sets `RepoState.path` and
    `RepoState.local` (computed once, present in both success and error paths).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_fetch.py` (imports at top: `from pathlib import Path`,
`from github_checker.models import LocalStatus, RepoRef`):

```python
@pytest.mark.anyio
async def test_fetch_repo_attaches_local_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gh, "_gh_api", _fake_gh_api(RESPONSES))
    monkeypatch.setattr(
        gh,
        "local_status",
        lambda path: LocalStatus(
            branch="main", ahead=1, behind=0, dirty=False, error=None
        ),
    )
    ref = RepoRef(name="o/r", path=Path("/tmp/o-r"))
    state = (await gh.fetch_all([ref]))[0]
    assert state.path == Path("/tmp/o-r")
    assert state.local is not None
    assert state.local.ahead == 1


@pytest.mark.anyio
async def test_fetch_repo_no_path_has_no_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gh, "_gh_api", _fake_gh_api(RESPONSES))
    state = (await gh.fetch_all(["o/r"]))[0]
    assert state.path is None
    assert state.local is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_fetch.py -k local -v`
Expected: FAIL — `fetch_all([ref])` raises (str expected) / `local_status`
attribute missing on `gh`.

- [ ] **Step 3: Update github.py**

Add imports near the top of `github_checker/github.py`:

```python
from github_checker.localgit import local_status
from github_checker.models import (
    Branch,
    CopilotReview,
    LocalStatus,
    PullRequest,
    RepoRef,
    RepoState,
    RulesetDetails,
    RulesetInfo,
)
```

(`LocalStatus` import keeps the symbol available for type reference/tests; keep
the existing model imports too.)

Change `fetch_repo`'s signature and the two `RepoState(...)` return sites, and
compute local status once up front:

```python
async def fetch_repo(ref: RepoRef, sem: asyncio.Semaphore) -> RepoState:
    """Fetch full state of one repository; errors go into RepoState.error."""
    name = ref.name
    local = (
        await asyncio.to_thread(local_status, ref.path)
        if ref.path is not None
        else None
    )

    async def call(path: str) -> Any:
        async with sem:
            return await _gh_api(path)

    try:
        # ... unchanged body up to the success return ...
        return RepoState(
            name=name,
            path=ref.path,
            local=local,
            pulls=pulls,
            branches=parse_branches(branches_json),
            alerts=alerts,
            rulesets=rulesets,
            updated_at=datetime.now(),
        )
    except GhError as err:
        return RepoState(name=name, path=ref.path, local=local, error=err.message)
    except Exception as err:
        return RepoState(
            name=name,
            path=ref.path,
            local=local,
            error=f"{type(err).__name__}: {err}",
        )
```

Update `fetch_all` to coerce and dispatch:

```python
async def fetch_all(repos: list[RepoRef | str]) -> list[RepoState]:
    """Fetch all repositories concurrently (bounded by MAX_CONCURRENCY)."""
    refs = [r if isinstance(r, RepoRef) else RepoRef(name=r) for r in repos]
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    return list(await asyncio.gather(*(fetch_repo(r, sem) for r in refs)))
```

- [ ] **Step 4: Run the whole fetch suite to verify pass + no regressions**

Run: `uv run pytest tests/test_fetch.py -v`
Expected: PASS (all — existing tests pass strings, still coerced).

- [ ] **Step 5: Type-check, format, commit**

```bash
uv run ruff format github_checker/github.py tests/test_fetch.py
uv run ruff check github_checker/github.py tests/test_fetch.py --fix
uv run pyrefly check
git add github_checker/github.py tests/test_fetch.py
git commit -m "feat: attach local clone status to RepoState on fetch"
```

---

### Task 5: Details pane — clickable link + local block

**Files:**
- Modify: `github_checker/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `RepoState.path`/`.local`, `LocalStatus` (Task 1).
- Produces:
  - `GITHUB_URL = "https://github.com/{name}"`.
  - `local_line(local: LocalStatus) -> str`.
  - `details_text(state: RepoState) -> str` (now includes URL + local lines).
  - `details_content(state: RepoState) -> Text` — same text with a `link`
    style span over the URL line.
- Also fixes `repos` membership checks and the `ProtectionScreen` call to use
  `RepoRef.name`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_app.py`, update the import line and add tests. Change:

```python
from github_checker.app import (
    GithubCheckerApp,
    details_content,
    details_text,
    local_line,
    repo_row,
)
from github_checker.models import (
    Branch,
    Config,
    CopilotReview,
    LocalStatus,
    PullRequest,
    RepoState,
    RepoRef,
    RulesetInfo,
)
```

Keep the existing `test_details_text` / `test_details_text_error` (still valid —
the badges and branch names remain in the text). Add:

```python
def test_details_text_includes_link() -> None:
    assert "https://github.com/o/r" in details_text(STATE)


def test_details_content_has_link_span() -> None:
    content = details_content(STATE)
    url = "https://github.com/o/r"
    assert url in content.plain
    assert any(span.style == f"link {url}" for span in content.spans)


def test_local_line_variants() -> None:
    up = LocalStatus(branch="main", ahead=0, behind=0, dirty=False)
    assert "up to date" in local_line(up)
    none = LocalStatus(branch="main", ahead=None, behind=None, dirty=False)
    assert "no upstream" in local_line(none)
    desync = LocalStatus(branch="main", ahead=2, behind=1, dirty=True)
    assert "↑2" in local_line(desync) and "↓1" in local_line(desync)
    assert "dirty" in local_line(desync)
    err = LocalStatus(branch=None, ahead=None, behind=None, dirty=False,
                      error="boom")
    assert "boom" in local_line(err)


def test_details_text_shows_local_block() -> None:
    state = STATE.model_copy(
        update={
            "path": Path("/tmp/o-r"),
            "local": LocalStatus(
                branch="main", ahead=2, behind=1, dirty=False
            ),
        }
    )
    text = details_text(state)
    assert "Local: /tmp/o-r" in text
    assert "↑2 ↓1" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_app.py -k "details or local_line" -v`
Expected: FAIL — `ImportError` on `details_content`/`local_line`.

- [ ] **Step 3: Implement rendering changes in app.py**

Add import near the top of `github_checker/app.py`:

```python
from rich.text import Text
```

and extend the models import to include `LocalStatus` and `RepoRef`:

```python
from github_checker.models import LocalStatus, RepoRef, RepoState, RulesetInfo
```

Add the constant near `COLUMNS`:

```python
GITHUB_URL = "https://github.com/{name}"
```

Add `local_line` and `details_content`, and rewrite `details_text` (replace the
whole existing `details_text` function):

```python
def local_line(local: LocalStatus) -> str:
    """One-line desync summary for the details pane."""
    if local.error:
        return f"  ERROR: {local.error}"
    parts = [local.branch or "?"]
    if local.ahead is None or local.behind is None:
        parts.append("no upstream")
    elif local.ahead == 0 and local.behind == 0:
        parts.append("up to date")
    else:
        parts.append(f"↑{local.ahead} ↓{local.behind}")
    if local.dirty:
        parts.append("dirty")
    return "  " + "  ".join(parts)


def details_text(state: RepoState) -> str:
    """Plain-text details panel for one repository."""
    url = GITHUB_URL.format(name=state.name)
    if state.error:
        return "\n".join([state.name, "", url, "", f"ERROR: {state.error}"])
    lines = [state.name, url]
    if state.path is not None:
        lines += ["", f"Local: {state.path}"]
        if state.local is not None:
            lines.append(local_line(state.local))
    lines += ["", "Pull requests:"]
    if not state.pulls:
        lines.append("  (none)")
    for pull in state.pulls:
        badges = ""
        if pull.is_dependabot:
            badges += " [dbot]"
        if pull.copilot_review:
            label = _COPILOT_STATE_LABELS.get(
                pull.copilot_review.state, pull.copilot_review.state.lower()
            )
            badges += f" [copilot: {label} ({pull.copilot_review.comment_count})]"
        lines.append(f"  #{pull.number} {pull.title} ({pull.author}){badges}")
    lines += ["", "Branches:"]
    if not state.branches:
        lines.append("  (none)")
    lines += [f"  {branch.name}" for branch in state.branches]
    return "\n".join(lines)


def details_content(state: RepoState) -> Text:
    """Rich Text of the details panel with the GitHub URL as a click link."""
    url = GITHUB_URL.format(name=state.name)
    text = Text()
    for line in details_text(state).split("\n"):
        if line == url:
            text.append(line, style=f"link {url}")
        else:
            text.append(line)
        text.append("\n")
    return text
```

Update `_show_details` to render the Text renderable:

```python
    def _show_details(self) -> None:
        details = self.query_one("#details", Static)
        state = self._states.get(self._selected) if self._selected else None
        if state is None:
            details.update("Нет репозиториев. Нажмите 'a', чтобы добавить.")
            return
        details.update(details_content(state))
```

Fix the `repos` membership check in `action_add_repo` (`handle_result`):

```python
            if any(ref.name == name for ref in self._config.repos):
                self.notify(f"{name} уже в списке", severity="information")
                return
```

Fix the `ProtectionScreen` construction in `action_protection`:

```python
        self.push_screen(
            ProtectionScreen(name, [r.name for r in self._config.repos]),
            handle_result,
        )
```

- [ ] **Step 4: Run the app suite to verify pass + no regressions**

Run: `uv run pytest tests/test_app.py -v`
Expected: PASS (all).

- [ ] **Step 5: Type-check, format, commit**

```bash
uv run ruff format github_checker/app.py tests/test_app.py
uv run ruff check github_checker/app.py tests/test_app.py --fix
uv run pyrefly check
git add github_checker/app.py tests/test_app.py
git commit -m "feat: clickable repo link and local status in details pane"
```

---

### Task 6: Sync hotkeys (`s` fetch, `S` pull)

**Files:**
- Modify: `github_checker/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `localgit.fetch`, `localgit.pull_ff_only`, `localgit.local_status`,
  `localgit.LocalGitError`; `RepoRef` from config.
- Produces: `action_sync`, `action_pull`, `_selected_ref`, `_run_local`,
  `_do_local` on `GithubCheckerApp`; bindings `s`/`S`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_app.py`:

```python
@pytest.mark.anyio
async def test_sync_updates_local_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    monkeypatch.setattr(app_module.localgit, "fetch", lambda path: None)
    monkeypatch.setattr(
        app_module.localgit,
        "local_status",
        lambda path: LocalStatus(
            branch="main", ahead=0, behind=0, dirty=False, error=None
        ),
    )
    config_path = tmp_path / "repos.toml"
    save_config(
        config_path,
        Config(repos=[RepoRef(name="o/r", path=tmp_path / "clone")]),
    )
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        app.apply_states([STATE.model_copy(update={"name": "o/r"})])
        await pilot.pause()
        await pilot.press("s")
        await pilot.pause()
        assert app._states["o/r"].local is not None
        assert app._states["o/r"].local.branch == "main"


@pytest.mark.anyio
async def test_sync_without_path_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    notes: list[str] = []
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        app.apply_states([STATE.model_copy(update={"name": "o/r"})])
        await pilot.pause()
        monkeypatch.setattr(app, "notify", lambda *a, **k: notes.append(a[0]))
        await pilot.press("s")
        await pilot.pause()
        assert any("локальный путь" in n for n in notes)
        assert app._states["o/r"].local is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_app.py -k sync -v`
Expected: FAIL — no `s` binding / `localgit` attribute on `app_module`.

- [ ] **Step 3: Implement sync actions**

Add imports near the top of `github_checker/app.py`:

```python
import asyncio

from github_checker import localgit
```

Add two bindings to `BINDINGS` (after the `"p"` entry):

```python
        ("s", "sync", "Sync"),
        ("S", "pull", "Pull"),
```

Add the methods to `GithubCheckerApp` (place after `action_protection`):

```python
    def _selected_ref(self) -> RepoRef | None:
        if self._selected is None:
            return None
        return next(
            (r for r in self._config.repos if r.name == self._selected), None
        )

    def action_sync(self) -> None:
        """Fetch the selected clone from its remote."""
        self._run_local(localgit.fetch, "fetch")

    def action_pull(self) -> None:
        """Fast-forward-only pull of the selected clone."""
        self._run_local(localgit.pull_ff_only, "pull")

    def _run_local(
        self, op: "Callable[[Path], None]", label: str
    ) -> None:
        ref = self._selected_ref()
        if ref is None or ref.path is None:
            self.notify("локальный путь не задан", severity="warning")
            return
        self.run_worker(
            self._do_local(op, label, ref.name, ref.path),
            group="local",
            exclusive=False,
        )

    async def _do_local(
        self, op: "Callable[[Path], None]", label: str, name: str, path: Path
    ) -> None:
        try:
            await asyncio.to_thread(op, path)
        except localgit.LocalGitError as err:
            self.notify(f"{label} не удался: {err}", severity="error")
            return
        status = await asyncio.to_thread(localgit.local_status, path)
        state = self._states.get(name)
        if state is not None:
            state.local = status
            self.apply_states(list(self._states.values()))
        self.notify(f"{label}: {name} обновлён")
```

Add the `Callable` import to the top of the file (with the other imports):

```python
from collections.abc import Callable
```

- [ ] **Step 4: Run the app suite to verify pass + no regressions**

Run: `uv run pytest tests/test_app.py -v`
Expected: PASS (all).

- [ ] **Step 5: Type-check, format, commit**

```bash
uv run ruff format github_checker/app.py tests/test_app.py
uv run ruff check github_checker/app.py tests/test_app.py --fix
uv run pyrefly check
git add github_checker/app.py tests/test_app.py
git commit -m "feat: s/S hotkeys to fetch and ff-only pull a local clone"
```

---

### Task 7: Document the config format and run the full suite

**Files:**
- Modify: `repos.toml.example`
- Modify: `README.md` (keybindings + local path note, if such a section exists)

**Interfaces:** none (documentation + final verification).

- [ ] **Step 1: Update `repos.toml.example`**

Replace the contents with both forms (scalar first so the TOML is valid):

```toml
refresh_seconds = 120

# A repository with a local clone: `s` fetches it, `S` does an ff-only pull,
# and the details pane shows ↑ahead / ↓behind / dirty vs its upstream.
[[repos]]
name = "andrei-shtanakov/atp-platform"
path = "/Users/you/labs/atp-platform"

# Remote-only: just a clickable link and PR/branch/ruleset status.
[[repos]]
name = "andrei-shtanakov/Maestro"
```

- [ ] **Step 2: Update README keybindings (only if a keys/usage section exists)**

Read `README.md`; if it lists the `r`/`a`/`d`/`p`/`q` keys, add:

```markdown
- `s` — fetch the selected repo's local clone (safe)
- `S` — fast-forward-only pull of the local clone
```

and one sentence noting the optional `path` field in `repos.toml`. If no such
section exists, skip this step.

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest`
Expected: PASS (all). No test references `list[str]` repos or the old
`details_text` string-only shape.

- [ ] **Step 4: Full type-check and lint**

```bash
uv run ruff format .
uv run ruff check . --fix
uv run pyrefly check
```
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add repos.toml.example README.md
git commit -m "docs: document local clone path and sync keys"
```

---

## Self-Review Notes

- **Spec coverage:** clickable link (Task 5), optional local path (Tasks 1–2,
  5), desync info (Tasks 1/3/4/5), sync keys with fetch + ff-only (Task 6),
  backward-compatible config (Tasks 1–2), tests for every unit (all tasks).
- **Backward compat:** legacy `repos = ["a/b"]` still loads (before-validator);
  `fetch_all` still accepts `str` items (coercion) so untouched fetch tests pass.
- **Type consistency:** `local_status`, `fetch`, `pull_ff_only`, `LocalGitError`,
  `RepoRef`, `LocalStatus`, `details_content`, `local_line`, `GITHUB_URL` are
  used with the same names/signatures across tasks.
- **TOML ordering caveat:** `refresh_seconds` must precede `[[repos]]`;
  `tomli_w` emits scalars first, and Task 2 Step 4 explicitly checks the
  round-trip.
