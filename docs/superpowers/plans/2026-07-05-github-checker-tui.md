# github-checker TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Textual-дашборд, отслеживающий несколько GitHub-реп: открытые PRы (с пометкой dependabot), ветки, security alerts и статус Copilot-ревью.

**Architecture:** Пакет `github_checker/` из 5 модулей. Слой данных (`github.py`) вызывает `gh api` через `asyncio.subprocess` и возвращает `list[RepoState]`; TUI (`app.py`) рендерит их и ничего не знает о subprocess. Конфиг — `repos.toml` (pydantic + tomllib/tomli-w), редактируется и из TUI.

**Tech Stack:** Python 3.12, Textual, pydantic v2, tomli-w, pytest + anyio, gh CLI (внешняя зависимость).

**Spec:** `docs/superpowers/specs/2026-07-05-github-checker-tui-design.md`

## Global Constraints

- Пакеты ставить ТОЛЬКО через `uv add` / `uv add --dev` (никакого pip).
- Все функции с type hints; после каждой задачи: `uv run ruff format .`, `uv run ruff check .`, `uv run pyrefly check` — всё должно быть зелёным перед коммитом.
- Line length 88.
- Async-тесты — anyio (`@pytest.mark.anyio` + фикстура `anyio_backend="asyncio"`), НЕ pytest-asyncio.
- Тесты не ходят в сеть: `_gh_api` / `fetch_all` / `subprocess.run` всегда мокаются.
- Логины ботов (точные строки): dependabot — `dependabot[bot]`, copilot — `copilot-pull-request-reviewer[bot]`.
- Каждый коммит завершается трейлером:
  ```
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01RHf1sQf1ekSY5MfEkbM2L3
  ```

## File Structure

```
github_checker/
  __init__.py      # пустой, docstring
  models.py        # Config, Branch, CopilotReview, PullRequest, RepoState
  config.py        # load_config, save_config, add_repo, remove_repo
  github.py        # parse_*, GhError, _gh_api, fetch_repo, fetch_all, gh_ready
  app.py           # repo_row, details_text, GithubCheckerApp, модалки
  main.py          # entrypoint (argparse --config, preflight gh_ready)
tests/
  conftest.py      # anyio_backend fixture
  fixtures.py      # JSON-ответы API как python-словари
  test_models.py
  test_config.py
  test_parsers.py
  test_fetch.py
  test_app.py
  test_main.py
repos.toml         # стартовый конфиг (3 репы пользователя)
```

Корневой `main.py` (заглушка uv) удаляется в Task 7.

---

### Task 1: Setup + models.py

**Files:**
- Modify: `pyproject.toml` (deps через uv)
- Create: `github_checker/__init__.py`, `github_checker/models.py`
- Test: `tests/conftest.py`, `tests/test_models.py`

**Interfaces:**
- Produces:
  - `Config(repos: list[str] = [], refresh_seconds: int = 120)` — валидирует формат `owner/repo`, бросает `pydantic.ValidationError`.
  - `Branch(name: str)`
  - `CopilotReview(state: str, comment_count: int)`
  - `PullRequest(number: int, title: str, author: str, head_branch: str, is_dependabot: bool, copilot_review: CopilotReview | None = None)`
  - `RepoState(name: str, pulls: list[PullRequest] = [], branches: list[Branch] = [], alerts: int | None = None, error: str | None = None, updated_at: datetime | None = None)`

- [ ] **Step 1: Установить зависимости и каркас**

```bash
uv add textual pydantic tomli-w
uv add --dev pytest anyio pyrefly ruff
mkdir -p github_checker tests
```

В `pyproject.toml` добавить (секции, не заменяя существующее):

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.ruff]
line-length = 88
```

Затем `uv run pyrefly init` (принять сгенерированный конфиг).

- [ ] **Step 2: conftest + failing test**

`tests/conftest.py`:

```python
import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
```

`tests/test_models.py`:

```python
import pytest
from pydantic import ValidationError

from github_checker.models import Config, PullRequest, RepoState


def test_config_defaults() -> None:
    config = Config(repos=["owner/repo"])
    assert config.refresh_seconds == 120


def test_config_rejects_bad_repo() -> None:
    with pytest.raises(ValidationError):
        Config(repos=["not-a-repo"])


def test_repo_state_defaults() -> None:
    state = RepoState(name="o/r")
    assert state.pulls == []
    assert state.alerts is None
    assert state.error is None


def test_pull_request_optional_copilot() -> None:
    pr = PullRequest(
        number=1,
        title="t",
        author="a",
        head_branch="b",
        is_dependabot=False,
    )
    assert pr.copilot_review is None
```

- [ ] **Step 3: Запустить — убедиться, что падает**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL / ERROR — `ModuleNotFoundError: github_checker.models`

- [ ] **Step 4: Реализация**

`github_checker/__init__.py`:

```python
"""TUI monitor for multiple GitHub repositories."""
```

`github_checker/models.py`:

```python
"""Pydantic models for config and repository state."""

import re
from datetime import datetime

from pydantic import BaseModel, field_validator

REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


class Config(BaseModel):
    """Application configuration stored in repos.toml."""

    repos: list[str] = []
    refresh_seconds: int = 120

    @field_validator("repos")
    @classmethod
    def _validate_repos(cls, value: list[str]) -> list[str]:
        for repo in value:
            if not REPO_RE.match(repo):
                raise ValueError(f"invalid repo (expected owner/repo): {repo!r}")
        return value


class Branch(BaseModel):
    """A git branch."""

    name: str


class CopilotReview(BaseModel):
    """Summary of GitHub Copilot's review on a pull request."""

    state: str
    comment_count: int


class PullRequest(BaseModel):
    """An open pull request."""

    number: int
    title: str
    author: str
    head_branch: str
    is_dependabot: bool
    copilot_review: CopilotReview | None = None


class RepoState(BaseModel):
    """Everything the TUI shows about one repository."""

    name: str
    pulls: list[PullRequest] = []
    branches: list[Branch] = []
    alerts: int | None = None
    error: str | None = None
    updated_at: datetime | None = None
```

- [ ] **Step 5: Тесты зелёные, линт, коммит**

```bash
uv run pytest tests/test_models.py -v          # Expected: 4 passed
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat: project skeleton and pydantic models" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RHf1sQf1ekSY5MfEkbM2L3"
```

---

### Task 2: config.py — load/save/add/remove

**Files:**
- Create: `github_checker/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `Config` из Task 1.
- Produces:
  - `load_config(path: Path) -> Config` — если файла нет, создаёт его с `Config()` и возвращает пустой конфиг.
  - `save_config(path: Path, config: Config) -> None`
  - `add_repo(path: Path, name: str) -> Config` — валидирует name (через Config), дубликаты игнорирует, пишет файл.
  - `remove_repo(path: Path, name: str) -> Config`

- [ ] **Step 1: Failing tests**

`tests/test_config.py`:

```python
from pathlib import Path

import pytest
from pydantic import ValidationError

from github_checker.config import add_repo, load_config, remove_repo, save_config
from github_checker.models import Config


def test_load_missing_creates_empty(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    config = load_config(path)
    assert config.repos == []
    assert path.exists()


def test_save_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=["o/r"], refresh_seconds=30))
    loaded = load_config(path)
    assert loaded.repos == ["o/r"]
    assert loaded.refresh_seconds == 30


def test_add_repo(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=["o/r"]))
    config = add_repo(path, "o/two")
    assert config.repos == ["o/r", "o/two"]
    assert load_config(path).repos == ["o/r", "o/two"]


def test_add_repo_duplicate_noop(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=["o/r"]))
    assert add_repo(path, "o/r").repos == ["o/r"]


def test_add_repo_invalid_raises(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config())
    with pytest.raises(ValidationError):
        add_repo(path, "garbage")


def test_remove_repo(tmp_path: Path) -> None:
    path = tmp_path / "repos.toml"
    save_config(path, Config(repos=["o/r", "o/two"]))
    config = remove_repo(path, "o/r")
    assert config.repos == ["o/two"]
    assert load_config(path).repos == ["o/two"]
```

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_config.py -v`
Expected: ERROR — `ModuleNotFoundError: github_checker.config`

- [ ] **Step 3: Реализация**

`github_checker/config.py`:

```python
"""Load and persist repos.toml."""

import tomllib
from pathlib import Path

import tomli_w

from github_checker.models import Config


def load_config(path: Path) -> Config:
    """Read config from *path*, creating an empty one if missing."""
    if not path.exists():
        config = Config()
        save_config(path, config)
        return config
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return Config(**data)


def save_config(path: Path, config: Config) -> None:
    """Write *config* to *path* as TOML."""
    path.write_text(tomli_w.dumps(config.model_dump()), encoding="utf-8")


def add_repo(path: Path, name: str) -> Config:
    """Add *name* to the config file; duplicates are ignored."""
    config = load_config(path)
    if name in config.repos:
        return config
    updated = Config(
        repos=[*config.repos, name],
        refresh_seconds=config.refresh_seconds,
    )
    save_config(path, updated)
    return updated


def remove_repo(path: Path, name: str) -> Config:
    """Remove *name* from the config file if present."""
    config = load_config(path)
    updated = config.model_copy(
        update={"repos": [r for r in config.repos if r != name]}
    )
    save_config(path, updated)
    return updated
```

- [ ] **Step 4: Тесты зелёные, линт, коммит**

```bash
uv run pytest tests/test_config.py -v          # Expected: 6 passed
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat: TOML config load/save with add/remove helpers" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RHf1sQf1ekSY5MfEkbM2L3"
```

---

### Task 3: github.py — чистые парсеры JSON → модели

**Files:**
- Create: `github_checker/github.py` (парсеры; async-часть — Task 4)
- Create: `tests/fixtures.py`
- Test: `tests/test_parsers.py`

**Interfaces:**
- Consumes: модели из Task 1.
- Produces:
  - `DEPENDABOT_LOGIN = "dependabot[bot]"`, `COPILOT_LOGIN = "copilot-pull-request-reviewer[bot]"`
  - `parse_pull(data: dict[str, Any]) -> PullRequest` (без copilot_review — его доклеивает fetch)
  - `parse_branches(data: list[dict[str, Any]]) -> list[Branch]`
  - `copilot_state(reviews: list[dict[str, Any]]) -> str | None` — state последнего ревью Copilot или None.
  - `count_copilot_comments(comments: list[dict[str, Any]]) -> int`

- [ ] **Step 1: Фикстуры**

`tests/fixtures.py` — усечённые до нужных полей реальные формы ответов REST API:

```python
"""Trimmed copies of real GitHub REST API response shapes."""

from typing import Any

PULLS: list[dict[str, Any]] = [
    {
        "number": 42,
        "title": "Add feature X",
        "user": {"login": "andrei-shtanakov"},
        "head": {"ref": "feature-x"},
    },
    {
        "number": 43,
        "title": "Bump httpx from 0.27.0 to 0.28.1",
        "user": {"login": "dependabot[bot]"},
        "head": {"ref": "dependabot/pip/httpx-0.28.1"},
    },
]

BRANCHES: list[dict[str, Any]] = [
    {"name": "master", "protected": True},
    {"name": "feature-x", "protected": False},
]

REVIEWS_WITH_COPILOT: list[dict[str, Any]] = [
    {"user": {"login": "some-human"}, "state": "APPROVED"},
    {
        "user": {"login": "copilot-pull-request-reviewer[bot]"},
        "state": "COMMENTED",
    },
]

REVIEWS_NO_COPILOT: list[dict[str, Any]] = [
    {"user": {"login": "some-human"}, "state": "APPROVED"},
]

REVIEW_COMMENTS: list[dict[str, Any]] = [
    {"user": {"login": "copilot-pull-request-reviewer[bot]"}, "body": "nit: ..."},
    {"user": {"login": "copilot-pull-request-reviewer[bot]"}, "body": "typo"},
    {"user": {"login": "some-human"}, "body": "lgtm"},
]

ALERTS: list[dict[str, Any]] = [
    {"number": 1, "state": "open"},
    {"number": 2, "state": "open"},
]
```

- [ ] **Step 2: Failing tests**

`tests/test_parsers.py`:

```python
from github_checker.github import (
    copilot_state,
    count_copilot_comments,
    parse_branches,
    parse_pull,
)
from tests.fixtures import (
    BRANCHES,
    PULLS,
    REVIEW_COMMENTS,
    REVIEWS_NO_COPILOT,
    REVIEWS_WITH_COPILOT,
)


def test_parse_pull_regular() -> None:
    pr = parse_pull(PULLS[0])
    assert pr.number == 42
    assert pr.author == "andrei-shtanakov"
    assert pr.head_branch == "feature-x"
    assert not pr.is_dependabot


def test_parse_pull_dependabot() -> None:
    assert parse_pull(PULLS[1]).is_dependabot


def test_parse_branches() -> None:
    branches = parse_branches(BRANCHES)
    assert [b.name for b in branches] == ["master", "feature-x"]


def test_copilot_state_found() -> None:
    assert copilot_state(REVIEWS_WITH_COPILOT) == "COMMENTED"


def test_copilot_state_absent() -> None:
    assert copilot_state(REVIEWS_NO_COPILOT) is None


def test_count_copilot_comments() -> None:
    assert count_copilot_comments(REVIEW_COMMENTS) == 2
```

- [ ] **Step 3: Запустить — падает**

Run: `uv run pytest tests/test_parsers.py -v`
Expected: ERROR — `ModuleNotFoundError: github_checker.github`

- [ ] **Step 4: Реализация**

`github_checker/github.py`:

```python
"""Fetch repository state via the gh CLI."""

from typing import Any

from github_checker.models import Branch, PullRequest

DEPENDABOT_LOGIN = "dependabot[bot]"
COPILOT_LOGIN = "copilot-pull-request-reviewer[bot]"


def parse_pull(data: dict[str, Any]) -> PullRequest:
    """Map one item of GET repos/{r}/pulls to a model."""
    login = data["user"]["login"]
    return PullRequest(
        number=data["number"],
        title=data["title"],
        author=login,
        head_branch=data["head"]["ref"],
        is_dependabot=login == DEPENDABOT_LOGIN,
    )


def parse_branches(data: list[dict[str, Any]]) -> list[Branch]:
    """Map GET repos/{r}/branches to models."""
    return [Branch(name=item["name"]) for item in data]


def copilot_state(reviews: list[dict[str, Any]]) -> str | None:
    """Return the state of Copilot's latest review, or None."""
    states = [
        r["state"] for r in reviews if r.get("user", {}).get("login") == COPILOT_LOGIN
    ]
    return states[-1] if states else None


def count_copilot_comments(comments: list[dict[str, Any]]) -> int:
    """Count review comments authored by Copilot."""
    return sum(
        1 for c in comments if c.get("user", {}).get("login") == COPILOT_LOGIN
    )
```

- [ ] **Step 5: Тесты зелёные, линт, коммит**

```bash
uv run pytest tests/test_parsers.py -v         # Expected: 6 passed
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat: parsers for GitHub API responses" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RHf1sQf1ekSY5MfEkbM2L3"
```

---

### Task 4: github.py — async fetch через gh CLI

**Files:**
- Modify: `github_checker/github.py` (добавить в конец)
- Test: `tests/test_fetch.py`

**Interfaces:**
- Consumes: парсеры из Task 3, модели из Task 1.
- Produces:
  - `GhError(Exception)` с атрибутами `status: int | None`, `message: str`.
  - `async _gh_api(path: str) -> Any` — один вызов `gh api {path}`, JSON или GhError.
  - `async fetch_repo(name: str, sem: asyncio.Semaphore) -> RepoState` — никогда не бросает GhError, ошибки складывает в `RepoState.error`.
  - `async fetch_all(repos: list[str]) -> list[RepoState]` — семафор на 8, порядок = порядок repos.
  - `gh_ready() -> str | None` — None если `gh auth status` ок, иначе текст ошибки.

- [ ] **Step 1: Failing tests**

`tests/test_fetch.py`:

```python
import subprocess
from typing import Any

import pytest

import github_checker.github as gh
from tests.fixtures import (
    ALERTS,
    BRANCHES,
    PULLS,
    REVIEW_COMMENTS,
    REVIEWS_WITH_COPILOT,
)

RESPONSES: dict[str, Any] = {
    "repos/o/r/pulls?state=open&per_page=100": PULLS,
    "repos/o/r/branches?per_page=100": BRANCHES,
    "repos/o/r/pulls/42/reviews?per_page=100": REVIEWS_WITH_COPILOT,
    "repos/o/r/pulls/42/comments?per_page=100": REVIEW_COMMENTS,
    "repos/o/r/pulls/43/reviews?per_page=100": [],
    "repos/o/r/dependabot/alerts?state=open&per_page=100": ALERTS,
}


def _fake_gh_api(
    responses: dict[str, Any], forbidden: set[str] | None = None
) -> Any:
    async def fake(path: str) -> Any:
        if forbidden and path in forbidden:
            raise gh.GhError(403, "HTTP 403: Forbidden")
        if path not in responses:
            raise gh.GhError(404, "HTTP 404: Not Found")
        return responses[path]

    return fake


@pytest.mark.anyio
async def test_fetch_repo_full(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gh, "_gh_api", _fake_gh_api(RESPONSES))
    state = (await gh.fetch_all(["o/r"]))[0]
    assert state.error is None
    assert len(state.pulls) == 2
    assert state.pulls[1].is_dependabot
    assert state.alerts == 2
    assert state.updated_at is not None
    copilot = state.pulls[0].copilot_review
    assert copilot is not None
    assert copilot.state == "COMMENTED"
    assert copilot.comment_count == 2
    assert state.pulls[1].copilot_review is None


@pytest.mark.anyio
async def test_fetch_repo_alerts_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden = {"repos/o/r/dependabot/alerts?state=open&per_page=100"}
    monkeypatch.setattr(gh, "_gh_api", _fake_gh_api(RESPONSES, forbidden))
    state = (await gh.fetch_all(["o/r"]))[0]
    assert state.error is None
    assert state.alerts is None


@pytest.mark.anyio
async def test_fetch_repo_error_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gh, "_gh_api", _fake_gh_api(RESPONSES))
    states = await gh.fetch_all(["o/r", "o/missing"])
    assert states[0].error is None
    assert states[1].error is not None
    assert states[1].name == "o/missing"


def test_gh_ready_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_fnf(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", raise_fnf)
    message = gh.gh_ready()
    assert message is not None and "gh" in message
```

Примечание: `pytestmark = pytest.mark.anyio` на весь файл НЕ использовать —
`test_gh_ready_missing_binary` синхронный; поэтому три async-теста помечены
декораторами индивидуально.

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_fetch.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'GhError'` (или ImportError)

- [ ] **Step 3: Реализация — добавить в конец `github_checker/github.py`**

Дополнить импорты вверху файла:

```python
import asyncio
import json
import re
import subprocess
from datetime import datetime

from github_checker.models import Branch, CopilotReview, PullRequest, RepoState
```

Добавить в конец:

```python
MAX_CONCURRENCY = 8
_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")


class GhError(Exception):
    """A failed gh CLI invocation."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


async def _gh_api(path: str) -> Any:
    """Run `gh api <path>` and return parsed JSON."""
    proc = await asyncio.create_subprocess_exec(
        "gh",
        "api",
        path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        text = stderr.decode().strip()
        match = _HTTP_STATUS_RE.search(text)
        status = int(match.group(1)) if match else None
        raise GhError(status, text or "gh api failed")
    return json.loads(stdout)


async def fetch_repo(name: str, sem: asyncio.Semaphore) -> RepoState:
    """Fetch full state of one repository; errors go into RepoState.error."""

    async def call(path: str) -> Any:
        async with sem:
            return await _gh_api(path)

    try:
        pulls_json, branches_json = await asyncio.gather(
            call(f"repos/{name}/pulls?state=open&per_page=100"),
            call(f"repos/{name}/branches?per_page=100"),
        )
        pulls = [parse_pull(item) for item in pulls_json]
        reviews_json = await asyncio.gather(
            *(call(f"repos/{name}/pulls/{p.number}/reviews?per_page=100") for p in pulls)
        )
        for pull, reviews in zip(pulls, reviews_json):
            state = copilot_state(reviews)
            if state is None:
                continue
            comments = await call(
                f"repos/{name}/pulls/{pull.number}/comments?per_page=100"
            )
            pull.copilot_review = CopilotReview(
                state=state,
                comment_count=count_copilot_comments(comments),
            )
        try:
            alerts_json = await call(
                f"repos/{name}/dependabot/alerts?state=open&per_page=100"
            )
            alerts: int | None = len(alerts_json)
        except GhError as err:
            if err.status not in (403, 404):
                raise
            alerts = None
        return RepoState(
            name=name,
            pulls=pulls,
            branches=parse_branches(branches_json),
            alerts=alerts,
            updated_at=datetime.now(),
        )
    except GhError as err:
        return RepoState(name=name, error=err.message)


async def fetch_all(repos: list[str]) -> list[RepoState]:
    """Fetch all repositories concurrently (bounded by MAX_CONCURRENCY)."""
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    return list(await asyncio.gather(*(fetch_repo(r, sem) for r in repos)))


def gh_ready() -> str | None:
    """Return None if gh CLI is installed and authenticated, else a message."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True
        )
    except FileNotFoundError:
        return "gh CLI не найден. Установите его: https://cli.github.com"
    if result.returncode != 0:
        return (
            "gh не авторизован. Выполните `gh auth login`.\n"
            + result.stderr.strip()
        )
    return None
```

- [ ] **Step 4: Тесты зелёные, линт, коммит**

```bash
uv run pytest tests/test_fetch.py -v           # Expected: 4 passed
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat: async repo fetching via gh CLI" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RHf1sQf1ekSY5MfEkbM2L3"
```

---

### Task 5: app.py — рендер-хелперы и базовое Textual-приложение

**Files:**
- Create: `github_checker/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `load_config` (Task 2), `fetch_all` (Task 4), модели (Task 1).
- Produces:
  - `repo_row(state: RepoState) -> tuple[str, str, str, str, str, str, str]` — колонки `Repo, PRs, Bot, Branches, Alerts, Copilot, Updated`; при `error` все счётчики `-`, Updated=`error`; `alerts is None` → `n/a`; счётчики >= 100 → `100+`.
  - `details_text(state: RepoState) -> str` — plain text (без Rich-markup).
  - `GithubCheckerApp(config_path: Path)` с методом `apply_states(states: list[RepoState]) -> None` и действиями `action_refresh`, `action_add_repo` (Task 6), `action_remove_repo` (Task 6).

- [ ] **Step 1: Failing tests (хелперы + рендер таблицы)**

`tests/test_app.py`:

```python
from datetime import datetime
from pathlib import Path

import pytest
from textual.widgets import DataTable

import github_checker.app as app_module
from github_checker.app import GithubCheckerApp, details_text, repo_row
from github_checker.config import save_config
from github_checker.models import (
    Branch,
    Config,
    CopilotReview,
    PullRequest,
    RepoState,
)

STATE = RepoState(
    name="o/r",
    pulls=[
        PullRequest(
            number=42,
            title="Add feature X",
            author="me",
            head_branch="feature-x",
            is_dependabot=False,
            copilot_review=CopilotReview(state="COMMENTED", comment_count=2),
        ),
        PullRequest(
            number=43,
            title="Bump httpx",
            author="dependabot[bot]",
            head_branch="dependabot/pip/httpx",
            is_dependabot=True,
        ),
    ],
    branches=[Branch(name="master"), Branch(name="feature-x")],
    alerts=None,
    updated_at=datetime(2026, 7, 5, 12, 0, 0),
)


def test_repo_row_normal() -> None:
    assert repo_row(STATE) == (
        "o/r",
        "2",
        "1",
        "2",
        "n/a",
        "1/2",
        "12:00:00",
    )


def test_repo_row_error() -> None:
    state = RepoState(name="o/bad", error="HTTP 404: Not Found")
    assert repo_row(state) == ("o/bad", "-", "-", "-", "-", "-", "error")


def test_repo_row_caps_at_100() -> None:
    state = RepoState(
        name="o/big",
        branches=[Branch(name=f"b{i}") for i in range(100)],
        updated_at=datetime(2026, 7, 5, 12, 0, 0),
    )
    assert repo_row(state)[3] == "100+"


def test_details_text() -> None:
    text = details_text(STATE)
    assert "#42 Add feature X (me) [copilot: commented (2)]" in text
    assert "#43 Bump httpx (dependabot[bot]) [dbot]" in text
    assert "master" in text


def test_details_text_error() -> None:
    text = details_text(RepoState(name="o/bad", error="boom"))
    assert "boom" in text


async def _noop_fetch_all(repos: list[str]) -> list[RepoState]:
    return []


@pytest.mark.anyio
async def test_app_renders_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        app.apply_states([STATE])
        await pilot.pause()
        table = app.query_one(DataTable)
        assert table.row_count == 1
```

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_app.py -v`
Expected: ERROR — `ModuleNotFoundError: github_checker.app`

- [ ] **Step 3: Реализация**

`github_checker/app.py`:

```python
"""Textual dashboard application."""

from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import DataTable, Footer, Header, Static

from github_checker.config import load_config
from github_checker.github import fetch_all
from github_checker.models import RepoState

COLUMNS = ("Repo", "PRs", "Bot", "Branches", "Alerts", "Copilot", "Updated")

_COPILOT_STATE_LABELS = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes",
    "COMMENTED": "commented",
}


def _count(n: int) -> str:
    return "100+" if n >= 100 else str(n)


def repo_row(state: RepoState) -> tuple[str, str, str, str, str, str, str]:
    """Build one table row for a repository."""
    if state.error:
        return (state.name, "-", "-", "-", "-", "-", "error")
    bot = sum(1 for p in state.pulls if p.is_dependabot)
    with_copilot = sum(1 for p in state.pulls if p.copilot_review)
    alerts = "n/a" if state.alerts is None else _count(state.alerts)
    updated = state.updated_at.strftime("%H:%M:%S") if state.updated_at else "-"
    return (
        state.name,
        _count(len(state.pulls)),
        str(bot),
        _count(len(state.branches)),
        alerts,
        f"{with_copilot}/{len(state.pulls)}",
        updated,
    )


def details_text(state: RepoState) -> str:
    """Plain-text details panel for one repository."""
    if state.error:
        return f"{state.name}\n\nERROR: {state.error}"
    lines = [state.name, "", "Pull requests:"]
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


class GithubCheckerApp(App[None]):
    """Dashboard showing the state of multiple GitHub repositories."""

    TITLE = "github-checker"
    CSS = """
    #table { width: 2fr; }
    #details-scroll { width: 1fr; border-left: solid $accent; padding: 0 1; }
    """
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("a", "add_repo", "Add repo"),
        ("d", "remove_repo", "Remove repo"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self._config_path = config_path
        self._config = load_config(config_path)
        self._states: dict[str, RepoState] = {}
        self._selected: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="table", cursor_type="row")
            with VerticalScroll(id="details-scroll"):
                yield Static("", id="details", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns(*COLUMNS)
        self.set_interval(self._config.refresh_seconds, self.action_refresh)
        self.action_refresh()

    def action_refresh(self) -> None:
        """Reload config from disk and refetch everything in the background."""
        self._config = load_config(self._config_path)
        self.run_worker(self._refresh(), exclusive=True)

    async def _refresh(self) -> None:
        self.sub_title = "refreshing…"
        states = await fetch_all(self._config.repos)
        self.apply_states(states)
        self.sub_title = ""

    def apply_states(self, states: list[RepoState]) -> None:
        """Replace table contents with freshly fetched states."""
        self._states = {s.name: s for s in states}
        table = self.query_one(DataTable)
        table.clear()
        for state in states:
            table.add_row(*repo_row(state), key=state.name)
        if self._selected not in self._states:
            self._selected = states[0].name if states else None
        self._show_details()

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        if event.row_key is not None and event.row_key.value is not None:
            self._selected = event.row_key.value
            self._show_details()

    def _show_details(self) -> None:
        details = self.query_one("#details", Static)
        state = self._states.get(self._selected) if self._selected else None
        if state is None:
            details.update("Нет репозиториев. Нажмите 'a', чтобы добавить.")
            return
        details.update(details_text(state))
```

- [ ] **Step 4: Тесты зелёные, линт, коммит**

```bash
uv run pytest tests/test_app.py -v             # Expected: 6 passed
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat: Textual dashboard with repo table and details panel" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RHf1sQf1ekSY5MfEkbM2L3"
```

---

### Task 6: Модалки add/remove с записью в конфиг

**Files:**
- Modify: `github_checker/app.py`
- Test: `tests/test_app.py` (добавить в конец)

**Interfaces:**
- Consumes: `add_repo`, `remove_repo` (Task 2); `GithubCheckerApp` (Task 5).
- Produces:
  - `AddRepoScreen(ModalScreen[str | None])` — Input `#repo-input`, кнопки `#ok`/`#cancel`; Enter в Input = OK.
  - `ConfirmScreen(ModalScreen[bool])` — кнопки `#yes`/`#no`.
  - Рабочие `action_add_repo` / `action_remove_repo` в `GithubCheckerApp`.

- [ ] **Step 1: Failing tests — добавить в конец `tests/test_app.py`**

```python
@pytest.mark.anyio
async def test_add_repo_writes_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        await pilot.press("a")
        await pilot.pause()
        await pilot.press(*"o/new")
        await pilot.press("enter")
        await pilot.pause()
    from github_checker.config import load_config

    assert load_config(config_path).repos == ["o/r", "o/new"]


@pytest.mark.anyio
async def test_remove_repo_writes_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        app.apply_states([STATE.model_copy(update={"name": "o/r"})])
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        await pilot.click("#yes")
        await pilot.pause()
    from github_checker.config import load_config

    assert load_config(config_path).repos == []
```

- [ ] **Step 2: Запустить — падают только новые тесты**

Run: `uv run pytest tests/test_app.py -v`
Expected: 2 новых FAIL (модалки не открываются / конфиг не меняется), старые 6 PASS.

- [ ] **Step 3: Реализация — добавить в `github_checker/app.py`**

Импорты дополнить:

```python
from pydantic import ValidationError
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static

from github_checker.config import add_repo, load_config, remove_repo
```

Перед `GithubCheckerApp` добавить:

```python
class AddRepoScreen(ModalScreen[str | None]):
    """Prompt for an owner/repo string."""

    CSS = """
    AddRepoScreen { align: center middle; }
    #dialog { width: 60; height: auto; border: thick $accent; padding: 1 2; }
    #dialog Horizontal { height: auto; align-horizontal: right; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Добавить репозиторий (owner/repo):")
            yield Input(placeholder="owner/repo", id="repo-input")
            with Horizontal():
                yield Button("Add", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            value = self.query_one("#repo-input", Input).value.strip()
            self.dismiss(value or None)
        else:
            self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """Yes/no confirmation dialog."""

    CSS = """
    ConfirmScreen { align: center middle; }
    #dialog { width: 60; height: auto; border: thick $accent; padding: 1 2; }
    #dialog Horizontal { height: auto; align-horizontal: right; }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._message)
            with Horizontal():
                yield Button("Yes", variant="error", id="yes")
                yield Button("No", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")
```

В `GithubCheckerApp` добавить методы:

```python
    def action_add_repo(self) -> None:
        def handle_result(name: str | None) -> None:
            if not name:
                return
            try:
                self._config = add_repo(self._config_path, name)
            except ValidationError:
                self.notify(
                    f"Некорректное имя: {name!r} (нужно owner/repo)",
                    severity="error",
                )
                return
            self.action_refresh()

        self.push_screen(AddRepoScreen(), handle_result)

    def action_remove_repo(self) -> None:
        name = self._selected
        if name is None:
            return

        def handle_result(confirmed: bool | None) -> None:
            if not confirmed:
                return
            self._config = remove_repo(self._config_path, name)
            self._selected = None
            self.action_refresh()

        self.push_screen(ConfirmScreen(f"Удалить {name}?"), handle_result)
```

- [ ] **Step 4: Все тесты зелёные, линт, коммит**

```bash
uv run pytest -v                                # Expected: all passed
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat: add/remove repos from the TUI with config persistence" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RHf1sQf1ekSY5MfEkbM2L3"
```

---

### Task 7: entrypoint, стартовый конфиг, финальная сборка

**Files:**
- Create: `github_checker/main.py`, `repos.toml`
- Modify: `pyproject.toml` (script), `README.md`
- Delete: `main.py` (корневая заглушка)
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `gh_ready` (Task 4), `GithubCheckerApp` (Task 5).
- Produces: консольная команда `github-checker [--config PATH]`.

- [ ] **Step 1: Failing test**

`tests/test_main.py`:

```python
import subprocess
from typing import Any

import pytest

import github_checker.main as main_module


def test_main_exits_when_gh_not_ready(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(main_module, "gh_ready", lambda: "gh не авторизован")
    monkeypatch.setattr("sys.argv", ["github-checker"])
    with pytest.raises(SystemExit) as excinfo:
        main_module.main()
    assert excinfo.value.code == 1
    assert "gh не авторизован" in capsys.readouterr().err
```

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_main.py -v`
Expected: ERROR — `ModuleNotFoundError: github_checker.main`

- [ ] **Step 3: Реализация**

`github_checker/main.py`:

```python
"""Console entrypoint."""

import argparse
import sys
from pathlib import Path

from github_checker.app import GithubCheckerApp
from github_checker.github import gh_ready


def main() -> None:
    """Parse args, verify gh CLI, run the dashboard."""
    parser = argparse.ArgumentParser(
        prog="github-checker",
        description="TUI monitor for multiple GitHub repositories.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("repos.toml"),
        help="path to repos.toml (default: ./repos.toml)",
    )
    args = parser.parse_args()
    error = gh_ready()
    if error is not None:
        print(error, file=sys.stderr)
        raise SystemExit(1)
    GithubCheckerApp(args.config).run()


if __name__ == "__main__":
    main()
```

`repos.toml`:

```toml
repos = [
    "andrei-shtanakov/atp-platform",
    "andrei-shtanakov/Maestro",
    "andrei-shtanakov/spec-runner",
]
refresh_seconds = 120
```

В `pyproject.toml` добавить:

```toml
[project.scripts]
github-checker = "github_checker.main:main"
```

Удалить корневую заглушку и обновить README:

```bash
rm main.py
```

`README.md`:

```markdown
# github-checker

TUI-дашборд состояния нескольких GitHub-репозиториев: открытые PRы
(с пометкой dependabot), ветки, security alerts и статус Copilot-ревью.

## Требования

- Python 3.12+, [uv](https://docs.astral.sh/uv/)
- Авторизованный [gh CLI](https://cli.github.com) (`gh auth login`)

## Запуск

    uv run github-checker            # конфиг ./repos.toml
    uv run github-checker --config path/to/repos.toml

## Клавиши

| Клавиша | Действие |
|---|---|
| `r` | обновить сейчас |
| `a` | добавить репозиторий |
| `d` | удалить выбранный |
| `q` | выход |

Список реп хранится в `repos.toml` и правится либо из TUI, либо руками.
```

- [ ] **Step 4: Полная проверка**

```bash
uv run pytest -v                                # Expected: all passed
uv run github-checker --help                    # Expected: usage с --config
uv run ruff format . && uv run ruff check . && uv run pyrefly check
```

- [ ] **Step 5: Ручная smoke-проверка (интерактивно, вне CI)**

Run: `uv run github-checker` — таблица с тремя репами заполняется, `q` выходит.
Если терминал недоступен исполнителю — пометить шаг как «требует ручной проверки пользователем» и не блокировать коммит.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: console entrypoint, starter config and README" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RHf1sQf1ekSY5MfEkbM2L3"
```
