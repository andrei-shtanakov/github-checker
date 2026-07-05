# Rulesets Column + Management Screen Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Колонка `Rules` (состояние GitHub rulesets) в таблице дашборда + экран управления по `p`: детали, вкл/выкл, копирование в другую репу, удаление.

**Architecture:** Слой данных расширяется: `_gh_api` учится PUT/POST/DELETE с JSON-телом через stdin; `fetch_repo` дополнительно тянет список rulesetов (ошибка → `None`, не роняет репу). UI: колонка в `app.py`, весь экран управления — новый модуль `protection.py` (Screen + пикер целевой репы), возврат результата в главное приложение через dismiss-callback.

**Tech Stack:** та же база — Python 3.12, Textual, pydantic v2, pytest + anyio, gh CLI.

**Spec:** `docs/superpowers/specs/2026-07-05-rulesets-management-design.md`

## Global Constraints

- Пакеты только через `uv add` (ничего нового ставить не нужно).
- После каждой задачи: `uv run ruff format .`, `uv run ruff check .`, `uv run pyrefly check` — зелёные перед коммитом. Line length 88.
- Async-тесты — anyio, индивидуальные `@pytest.mark.anyio` (НЕ файловый pytestmark: в файлах есть sync-тесты).
- Тесты не ходят в сеть: `_gh_api` / `fetch_all` / write-функции мокаются.
- Значения колонки `Rules` (точные строки): `✓{N}` активных; `off{N}` если активных нет, но rulesets есть; `-` если список пуст; `?` если `rulesets is None`; `-` в error-строке.
- Служебные поля, удаляемые при копировании ruleset (точный список): `id`, `source`, `source_type`, `created_at`, `updated_at`, `current_user_can_bypass`, `node_id`, `_links`.
- Каждый коммит завершается трейлером:
  ```
  Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01RHf1sQf1ekSY5MfEkbM2L3
  ```

## File Structure

```
github_checker/
  models.py        # + RulesetInfo, RulesetDetails; RepoState.rulesets
  github.py        # + парсеры rulesets, _gh_api(method, body), write-операции
  app.py           # + колонка Rules, биндинг p, action_protection, callback
  protection.py    # NEW: ProtectionScreen, RepoPickerScreen, protection_details_text
tests/
  fixtures.py      # + RULESETS_LIST, RULESET_DETAILS
  test_parsers.py  # + тесты парсеров rulesets
  test_fetch.py    # + тесты _gh_api(method/body) и интеграции в fetch_repo
  test_rulesets_ops.py  # NEW: write-операции
  test_app.py      # правки под 8-колоночную таблицу
  test_protection.py    # NEW: рендер деталей + экран
```

---

### Task 1: Модели и парсеры rulesets

**Files:**
- Modify: `github_checker/models.py` (добавить в конец; `RepoState` — новое поле)
- Modify: `github_checker/github.py` (парсеры — после `count_copilot_comments`, до `MAX_CONCURRENCY`)
- Modify: `tests/fixtures.py` (добавить в конец)
- Test: `tests/test_parsers.py` (добавить в конец)

**Interfaces:**
- Consumes: существующие модели.
- Produces:
  - `RulesetInfo(id: int, name: str, enforcement: str, target: str)`
  - `RulesetDetails(id, name, enforcement, target, include: list[str], exclude: list[str], rules: list[str], bypass: list[str])`
  - `RepoState.rulesets: list[RulesetInfo] | None = None`
  - `parse_ruleset_info(data: dict[str, Any]) -> RulesetInfo`
  - `parse_ruleset_details(data: dict[str, Any]) -> RulesetDetails`
  - `format_bypass_actor(actor: dict[str, Any]) -> str`

- [ ] **Step 1: Фикстуры — добавить в конец `tests/fixtures.py`**

```python
RULESETS_LIST: list[dict[str, Any]] = [
    {
        "id": 14708017,
        "name": "Default Branch Restriction",
        "target": "branch",
        "source_type": "Repository",
        "source": "andrei-shtanakov/atp-platform",
        "enforcement": "active",
        "node_id": "RRS_lACqUmVwb3NpdG9yec4r3xLbzgDgZ9E",
        "created_at": "2025-01-15T10:00:00.000+00:00",
        "updated_at": "2025-06-01T10:00:00.000+00:00",
    },
    {
        "id": 12637526,
        "name": "dei-protection",
        "target": "branch",
        "source_type": "Repository",
        "source": "andrei-shtanakov/atp-platform",
        "enforcement": "disabled",
    },
]

RULESET_DETAILS: dict[str, Any] = {
    "id": 14708017,
    "name": "Default Branch Restriction",
    "target": "branch",
    "source_type": "Repository",
    "source": "andrei-shtanakov/atp-platform",
    "enforcement": "active",
    "current_user_can_bypass": "always",
    "node_id": "RRS_lACqUmVwb3NpdG9yec4r3xLbzgDgZ9E",
    "conditions": {
        "ref_name": {"include": ["refs/heads/main"], "exclude": []}
    },
    "rules": [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        {"type": "update"},
        {
            "type": "pull_request",
            "parameters": {"required_approving_review_count": 0},
        },
    ],
    "bypass_actors": [
        {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"},
        {"actor_id": 946600, "actor_type": "Integration", "bypass_mode": "always"},
    ],
    "created_at": "2025-01-15T10:00:00.000+00:00",
    "updated_at": "2025-06-01T10:00:00.000+00:00",
    "_links": {"self": {"href": "https://api.github.com/..."}},
}
```

- [ ] **Step 2: Failing tests — добавить в конец `tests/test_parsers.py`**

```python
from github_checker.github import (
    format_bypass_actor,
    parse_ruleset_details,
    parse_ruleset_info,
)
from tests.fixtures import RULESET_DETAILS, RULESETS_LIST


def test_parse_ruleset_info() -> None:
    info = parse_ruleset_info(RULESETS_LIST[0])
    assert info.id == 14708017
    assert info.name == "Default Branch Restriction"
    assert info.enforcement == "active"
    assert info.target == "branch"


def test_parse_ruleset_details() -> None:
    details = parse_ruleset_details(RULESET_DETAILS)
    assert details.include == ["refs/heads/main"]
    assert details.exclude == []
    assert details.rules == [
        "deletion",
        "non_fast_forward",
        "update",
        "pull_request",
    ]
    assert details.bypass == ["admin (role), always", "app id=946600, always"]


def test_format_bypass_actor_variants() -> None:
    assert (
        format_bypass_actor(
            {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
        )
        == "admin (role), always"
    )
    assert (
        format_bypass_actor(
            {"actor_id": 2, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}
        )
        == "role id=2, pull_request"
    )
    assert (
        format_bypass_actor({"actor_type": "OrganizationAdmin", "actor_id": 1})
        == "org admin, always"
    )
    assert (
        format_bypass_actor({"actor_type": "Team", "actor_id": 9, "bypass_mode": "always"})
        == "team id=9, always"
    )
```

(Импорты добавить к существующим в начале файла, соблюдая сортировку ruff.)

- [ ] **Step 3: Запустить — падает**

Run: `uv run pytest tests/test_parsers.py -v`
Expected: ImportError — нет `parse_ruleset_info`

- [ ] **Step 4: Реализация**

В `github_checker/models.py` добавить в конец:

```python
class RulesetInfo(BaseModel):
    """Item of GET repos/{r}/rulesets."""

    id: int
    name: str
    enforcement: str
    target: str


class RulesetDetails(BaseModel):
    """GET repos/{r}/rulesets/{id} — fields the protection screen needs."""

    id: int
    name: str
    enforcement: str
    target: str
    include: list[str]
    exclude: list[str]
    rules: list[str]
    bypass: list[str]
```

В `RepoState` добавить поле (после `alerts`):

```python
    rulesets: list[RulesetInfo] | None = None
```

(`RulesetInfo` определить ДО `RepoState` либо перенести `RepoState` в конец файла — проще определить обе новые модели перед `RepoState`.)

В `github_checker/github.py` — импорт моделей дополнить `RulesetDetails, RulesetInfo`; после `count_copilot_comments` добавить:

```python
def parse_ruleset_info(data: dict[str, Any]) -> RulesetInfo:
    """Map one item of GET repos/{r}/rulesets to a model."""
    return RulesetInfo(
        id=data["id"],
        name=data["name"],
        enforcement=data["enforcement"],
        target=data.get("target", "branch"),
    )


def format_bypass_actor(actor: dict[str, Any]) -> str:
    """Human-readable bypass actor, e.g. 'admin (role), always'."""
    actor_type = actor.get("actor_type", "?")
    actor_id = actor.get("actor_id")
    mode = actor.get("bypass_mode", "always")
    if actor_type == "RepositoryRole":
        base = "admin (role)" if actor_id == 5 else f"role id={actor_id}"
    elif actor_type == "Integration":
        base = f"app id={actor_id}"
    elif actor_type == "Team":
        base = f"team id={actor_id}"
    elif actor_type == "OrganizationAdmin":
        base = "org admin"
    else:
        base = f"{actor_type} id={actor_id}"
    return f"{base}, {mode}"


def parse_ruleset_details(data: dict[str, Any]) -> RulesetDetails:
    """Map GET repos/{r}/rulesets/{id} to a model."""
    ref = (data.get("conditions") or {}).get("ref_name") or {}
    return RulesetDetails(
        id=data["id"],
        name=data["name"],
        enforcement=data["enforcement"],
        target=data.get("target", "branch"),
        include=ref.get("include", []),
        exclude=ref.get("exclude", []),
        rules=[rule["type"] for rule in data.get("rules", [])],
        bypass=[format_bypass_actor(a) for a in data.get("bypass_actors", [])],
    )
```

- [ ] **Step 5: Тесты зелёные, линт, коммит**

```bash
uv run pytest tests/test_parsers.py -v          # Expected: 9 passed
uv run pytest -q                                # Expected: 37 passed
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat: ruleset models and parsers" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RHf1sQf1ekSY5MfEkbM2L3"
```

---

### Task 2: `_gh_api` с method/body + rulesets в fetch_repo

**Files:**
- Modify: `github_checker/github.py` (`_gh_api`, `fetch_repo`)
- Test: `tests/test_fetch.py` (добавить в конец)

**Interfaces:**
- Consumes: `parse_ruleset_info` (Task 1), `RepoState.rulesets` (Task 1).
- Produces:
  - `async _gh_api(path: str, method: str = "GET", body: dict[str, Any] | None = None) -> Any` — body сериализуется в JSON и подаётся через `--input -`; пустой stdout → `None` (для DELETE). Существующие вызовы `_gh_api(path)` работают без изменений.
  - `fetch_repo` заполняет `RepoState.rulesets`; любая `GhError` на этом вызове → `rulesets=None`, репа НЕ в ошибке.

- [ ] **Step 1: Failing tests — добавить в конец `tests/test_fetch.py`**

```python
class FakeProc:
    def __init__(self, stdout: bytes = b"{}", returncode: int = 0) -> None:
        self.stdout_data = stdout
        self.returncode = returncode
        self.stdin_received: bytes | None = None

    async def communicate(
        self, input: bytes | None = None
    ) -> tuple[bytes, bytes]:
        self.stdin_received = input
        return self.stdout_data, b""


@pytest.mark.anyio
async def test_gh_api_post_with_body(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = FakeProc()
    recorded: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> FakeProc:
        recorded["args"] = args
        return proc

    monkeypatch.setattr(gh.asyncio, "create_subprocess_exec", fake_exec)
    result = await gh._gh_api(
        "repos/o/r/rulesets", method="POST", body={"name": "x"}
    )
    assert recorded["args"][0] == "gh"
    assert "-X" in recorded["args"]
    assert "POST" in recorded["args"]
    assert "--input" in recorded["args"]
    assert proc.stdin_received == b'{"name": "x"}'
    assert result == {}


@pytest.mark.anyio
async def test_gh_api_delete_empty_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proc = FakeProc(stdout=b"")

    async def fake_exec(*args: Any, **kwargs: Any) -> FakeProc:
        return proc

    monkeypatch.setattr(gh.asyncio, "create_subprocess_exec", fake_exec)
    result = await gh._gh_api("repos/o/r/rulesets/1", method="DELETE")
    assert result is None


@pytest.mark.anyio
async def test_fetch_repo_includes_rulesets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = dict(RESPONSES)
    responses["repos/o/r/rulesets?per_page=100"] = RULESETS_LIST
    monkeypatch.setattr(gh, "_gh_api", _fake_gh_api(responses))
    state = (await gh.fetch_all(["o/r"]))[0]
    assert state.rulesets is not None
    assert [r.enforcement for r in state.rulesets] == ["active", "disabled"]


@pytest.mark.anyio
async def test_fetch_repo_rulesets_forbidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gh, "_gh_api", _fake_gh_api(RESPONSES))
    state = (await gh.fetch_all(["o/r"]))[0]
    assert state.error is None
    assert state.rulesets is None
```

Импорт фикстур в начале файла дополнить `RULESETS_LIST`.
(В `RESPONSES` пути rulesets нет, `_fake_gh_api` кидает `GhError(404, ...)` — это и есть ветка «нет прав».)

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_fetch.py -v`
Expected: FAIL — `_gh_api` не принимает `method`; `state.rulesets is None` в includes-тесте.

- [ ] **Step 3: Реализация**

Заменить `_gh_api` в `github_checker/github.py`:

```python
async def _gh_api(
    path: str, method: str = "GET", body: dict[str, Any] | None = None
) -> Any:
    """Run `gh api <path>` and return parsed JSON (None on empty output)."""
    args = ["api", path]
    if method != "GET":
        args += ["-X", method]
    stdin_data: bytes | None = None
    if body is not None:
        args += ["--input", "-"]
        stdin_data = json.dumps(body).encode()
    proc = await asyncio.create_subprocess_exec(
        "gh",
        *args,
        stdin=asyncio.subprocess.PIPE if stdin_data is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(stdin_data)
    if proc.returncode != 0:
        text = stderr.decode().strip()
        match = _HTTP_STATUS_RE.search(text)
        status = int(match.group(1)) if match else None
        raise GhError(status, text or "gh api failed")
    if not stdout.strip():
        return None
    return json.loads(stdout)
```

В `fetch_repo`, после блока alerts (перед `return RepoState(...)`):

```python
        try:
            rulesets_json = await call(f"repos/{name}/rulesets?per_page=100")
            rulesets: list[RulesetInfo] | None = [
                parse_ruleset_info(item) for item in rulesets_json
            ]
        except GhError:
            rulesets = None
```

и в `RepoState(...)` добавить `rulesets=rulesets,`.

- [ ] **Step 4: Тесты зелёные, линт, коммит**

```bash
uv run pytest tests/test_fetch.py -v            # Expected: 10 passed
uv run pytest -q                                # Expected: 41 passed
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat: gh api write support and rulesets in repo fetch" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RHf1sQf1ekSY5MfEkbM2L3"
```

---

### Task 3: Write-операции над rulesets

**Files:**
- Modify: `github_checker/github.py` (добавить в конец)
- Test: `tests/test_rulesets_ops.py` (новый)

**Interfaces:**
- Consumes: `_gh_api(path, method, body)` (Task 2), `GhError`, парсеры (Task 1).
- Produces:
  - `async list_rulesets(repo: str) -> list[RulesetInfo]`
  - `async get_ruleset(repo: str, ruleset_id: int) -> RulesetDetails`
  - `async set_ruleset_enforcement(repo: str, ruleset_id: int, enforcement: str) -> None`
  - `async delete_ruleset(repo: str, ruleset_id: int) -> None`
  - `build_ruleset_copy(data: dict[str, Any]) -> dict[str, Any]` — чистая, убирает служебные поля
  - `async copy_ruleset(src_repo: str, ruleset_id: int, dst_repo: str) -> None` — retry с суффиксом ` (copy)` при 422

- [ ] **Step 1: Failing tests — `tests/test_rulesets_ops.py`**

```python
from typing import Any

import pytest

import github_checker.github as gh
from tests.fixtures import RULESET_DETAILS, RULESETS_LIST


def _recording_gh_api(
    responses: dict[str, Any], fail_first_post_with: int | None = None
) -> tuple[Any, list[tuple[str, str, dict[str, Any] | None]]]:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    post_count = {"n": 0}

    async def fake(
        path: str, method: str = "GET", body: dict[str, Any] | None = None
    ) -> Any:
        calls.append((path, method, body))
        if method == "POST" and fail_first_post_with is not None:
            post_count["n"] += 1
            if post_count["n"] == 1:
                raise gh.GhError(fail_first_post_with, "HTTP 422: name taken")
        return responses.get(path)

    return fake, calls


def test_build_ruleset_copy_strips_service_fields() -> None:
    body = gh.build_ruleset_copy(RULESET_DETAILS)
    for field in (
        "id",
        "source",
        "source_type",
        "created_at",
        "updated_at",
        "current_user_can_bypass",
        "node_id",
        "_links",
    ):
        assert field not in body
    assert body["name"] == "Default Branch Restriction"
    assert body["rules"] == RULESET_DETAILS["rules"]
    assert body["bypass_actors"] == RULESET_DETAILS["bypass_actors"]


@pytest.mark.anyio
async def test_list_and_get(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = _recording_gh_api(
        {
            "repos/o/r/rulesets?per_page=100": RULESETS_LIST,
            "repos/o/r/rulesets/14708017": RULESET_DETAILS,
        }
    )
    monkeypatch.setattr(gh, "_gh_api", fake)
    infos = await gh.list_rulesets("o/r")
    assert [i.id for i in infos] == [14708017, 12637526]
    details = await gh.get_ruleset("o/r", 14708017)
    assert details.rules[0] == "deletion"


@pytest.mark.anyio
async def test_set_enforcement_and_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, calls = _recording_gh_api({})
    monkeypatch.setattr(gh, "_gh_api", fake)
    await gh.set_ruleset_enforcement("o/r", 1, "disabled")
    await gh.delete_ruleset("o/r", 1)
    assert calls[0] == ("repos/o/r/rulesets/1", "PUT", {"enforcement": "disabled"})
    assert calls[1] == ("repos/o/r/rulesets/1", "DELETE", None)


@pytest.mark.anyio
async def test_copy_ruleset(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, calls = _recording_gh_api(
        {"repos/o/src/rulesets/14708017": RULESET_DETAILS}
    )
    monkeypatch.setattr(gh, "_gh_api", fake)
    await gh.copy_ruleset("o/src", 14708017, "o/dst")
    path, method, body = calls[-1]
    assert path == "repos/o/dst/rulesets"
    assert method == "POST"
    assert body is not None and "id" not in body


@pytest.mark.anyio
async def test_copy_ruleset_retries_on_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, calls = _recording_gh_api(
        {"repos/o/src/rulesets/14708017": RULESET_DETAILS},
        fail_first_post_with=422,
    )
    monkeypatch.setattr(gh, "_gh_api", fake)
    await gh.copy_ruleset("o/src", 14708017, "o/dst")
    posts = [c for c in calls if c[1] == "POST"]
    assert len(posts) == 2
    assert posts[1][2] is not None
    assert posts[1][2]["name"] == "Default Branch Restriction (copy)"


@pytest.mark.anyio
async def test_copy_ruleset_non_422_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, _ = _recording_gh_api(
        {"repos/o/src/rulesets/14708017": RULESET_DETAILS},
        fail_first_post_with=403,
    )
    monkeypatch.setattr(gh, "_gh_api", fake)
    with pytest.raises(gh.GhError):
        await gh.copy_ruleset("o/src", 14708017, "o/dst")
```

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_rulesets_ops.py -v`
Expected: AttributeError — нет `build_ruleset_copy`

- [ ] **Step 3: Реализация — добавить в конец `github_checker/github.py`**

```python
_COPY_STRIP_FIELDS = frozenset(
    {
        "id",
        "source",
        "source_type",
        "created_at",
        "updated_at",
        "current_user_can_bypass",
        "node_id",
        "_links",
    }
)


async def list_rulesets(repo: str) -> list[RulesetInfo]:
    """List rulesets of a repository."""
    data = await _gh_api(f"repos/{repo}/rulesets?per_page=100")
    return [parse_ruleset_info(item) for item in data]


async def get_ruleset(repo: str, ruleset_id: int) -> RulesetDetails:
    """Fetch full details of one ruleset."""
    return parse_ruleset_details(await _gh_api(f"repos/{repo}/rulesets/{ruleset_id}"))


async def set_ruleset_enforcement(
    repo: str, ruleset_id: int, enforcement: str
) -> None:
    """Set enforcement ('active' | 'disabled') of a ruleset."""
    await _gh_api(
        f"repos/{repo}/rulesets/{ruleset_id}",
        method="PUT",
        body={"enforcement": enforcement},
    )


async def delete_ruleset(repo: str, ruleset_id: int) -> None:
    """Delete a ruleset."""
    await _gh_api(f"repos/{repo}/rulesets/{ruleset_id}", method="DELETE")


def build_ruleset_copy(data: dict[str, Any]) -> dict[str, Any]:
    """Strip server-side fields from a ruleset body before POSTing a copy."""
    return {k: v for k, v in data.items() if k not in _COPY_STRIP_FIELDS}


async def copy_ruleset(src_repo: str, ruleset_id: int, dst_repo: str) -> None:
    """Copy a ruleset to another repository (retry with ' (copy)' on 422)."""
    data = await _gh_api(f"repos/{src_repo}/rulesets/{ruleset_id}")
    body = build_ruleset_copy(data)
    try:
        await _gh_api(f"repos/{dst_repo}/rulesets", method="POST", body=body)
    except GhError as err:
        if err.status != 422:
            raise
        body["name"] = f"{body['name']} (copy)"
        await _gh_api(f"repos/{dst_repo}/rulesets", method="POST", body=body)
```

- [ ] **Step 4: Тесты зелёные, линт, коммит**

```bash
uv run pytest tests/test_rulesets_ops.py -v     # Expected: 6 passed
uv run pytest -q                                # Expected: 47 passed
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat: ruleset write operations (toggle, copy, delete)" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RHf1sQf1ekSY5MfEkbM2L3"
```

---

### Task 4: Колонка `Rules` в таблице

**Files:**
- Modify: `github_checker/app.py` (`COLUMNS`, `repo_row`, новая `rules_cell`)
- Test: `tests/test_app.py` (правка существующих + новые)

**Interfaces:**
- Consumes: `RepoState.rulesets` (Task 1).
- Produces:
  - `rules_cell(rulesets: list[RulesetInfo] | None) -> str` — `✓N` / `offN` / `-` / `?`
  - `COLUMNS = ("Repo", "PRs", "Bot", "Branches", "Alerts", "Rules", "Copilot", "Updated")`
  - `repo_row` возвращает 8-кортеж (Rules — 6-я позиция, индекс 5).

- [ ] **Step 1: Правка тестов (RED)**

В `tests/test_app.py`:

1. Импорт: добавить `RulesetInfo` к импортам из `github_checker.models`.
2. `test_repo_row_normal` — ожидание:

```python
    assert repo_row(STATE) == (
        "o/r",
        "2",
        "1",
        "2",
        "n/a",
        "?",
        "1/2",
        "12:00:00",
    )
```

3. `test_repo_row_error` — ожидание:

```python
    assert repo_row(state) == ("o/bad", "-", "-", "-", "-", "-", "-", "error")
```

4. Добавить в конец файла:

```python
def _ri(ruleset_id: int, enforcement: str) -> RulesetInfo:
    return RulesetInfo(
        id=ruleset_id, name=f"rs{ruleset_id}", enforcement=enforcement, target="branch"
    )


def test_rules_cell_variants() -> None:
    from github_checker.app import rules_cell

    assert rules_cell(None) == "?"
    assert rules_cell([]) == "-"
    assert rules_cell([_ri(1, "active"), _ri(2, "disabled")]) == "✓1"
    assert rules_cell([_ri(1, "disabled"), _ri(2, "evaluate")]) == "off2"


def test_repo_row_rules_column() -> None:
    state = STATE.model_copy(update={"rulesets": [_ri(1, "active")]})
    assert repo_row(state)[5] == "✓1"
```

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_app.py -v`
Expected: FAIL — старые ожидания 7-кортежей и отсутствие `rules_cell`.

- [ ] **Step 3: Реализация в `github_checker/app.py`**

`COLUMNS`:

```python
COLUMNS = ("Repo", "PRs", "Bot", "Branches", "Alerts", "Rules", "Copilot", "Updated")
```

Импорт моделей: `from github_checker.models import RepoState, RulesetInfo`.

Новая функция после `_count`:

```python
def rules_cell(rulesets: list[RulesetInfo] | None) -> str:
    """Rules column value: ✓N active / offN present-but-off / - none / ? unknown."""
    if rulesets is None:
        return "?"
    active = sum(1 for r in rulesets if r.enforcement == "active")
    if active:
        return f"✓{active}"
    if rulesets:
        return f"off{len(rulesets)}"
    return "-"
```

`repo_row` — тип результата и обе ветки:

```python
def repo_row(state: RepoState) -> tuple[str, str, str, str, str, str, str, str]:
    """Build one table row for a repository."""
    if state.error:
        return (state.name, "-", "-", "-", "-", "-", "-", "error")
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
        rules_cell(state.rulesets),
        f"{with_copilot}/{len(state.pulls)}",
        updated,
    )
```

- [ ] **Step 4: Тесты зелёные, линт, коммит**

```bash
uv run pytest tests/test_app.py -v              # Expected: 12 passed
uv run pytest -q                                # Expected: 49 passed
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat: Rules column in dashboard table" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RHf1sQf1ekSY5MfEkbM2L3"
```

---

### Task 5: ProtectionScreen + биндинг `p`

**Files:**
- Create: `github_checker/protection.py`
- Modify: `github_checker/app.py` (BINDINGS, `action_protection`)
- Test: `tests/test_protection.py` (новый)

**Interfaces:**
- Consumes: `list_rulesets`, `get_ruleset`, `set_ruleset_enforcement`, `copy_ruleset`, `delete_ruleset` (Task 3); `ConfirmScreen` (app.py); `RulesetInfo`, `RulesetDetails` (Task 1).
- Produces:
  - `protection_details_text(details: RulesetDetails) -> str` — чистая
  - `RepoPickerScreen(ModalScreen[str | None])` — OptionList реп
  - `ProtectionScreen(Screen[list[RulesetInfo] | None])` — dismiss возвращает актуальный список rulesetов (или None, если не менялся/не загрузился)
  - В `GithubCheckerApp`: биндинг `("p", "protection", "Rulesets")` и `action_protection`

- [ ] **Step 1: Failing tests — `tests/test_protection.py`**

```python
from pathlib import Path

import pytest
from textual.widgets import DataTable

import github_checker.app as app_module
import github_checker.protection as protection_module
from github_checker.app import GithubCheckerApp
from github_checker.config import save_config
from github_checker.models import Config, RepoState, RulesetDetails, RulesetInfo
from github_checker.protection import ProtectionScreen, protection_details_text

DETAILS = RulesetDetails(
    id=1,
    name="Main protection",
    enforcement="active",
    target="branch",
    include=["~DEFAULT_BRANCH"],
    exclude=["refs/heads/wip"],
    rules=["deletion", "pull_request", "exotic_rule"],
    bypass=["admin (role), always"],
)

INFO = RulesetInfo(id=1, name="Main protection", enforcement="active", target="branch")


def test_protection_details_text() -> None:
    text = protection_details_text(DETAILS)
    assert "Main protection" in text
    assert "enforcement: active" in text
    assert "default" in text  # ~DEFAULT_BRANCH -> default
    assert "refs/heads/wip" in text
    assert "запрет удаления" in text
    assert "только через PR" in text
    assert "exotic_rule" in text  # неизвестный тип — как есть
    assert "admin (role), always" in text


def test_protection_details_text_empty_lists() -> None:
    details = DETAILS.model_copy(
        update={"include": [], "exclude": [], "rules": [], "bypass": []}
    )
    text = protection_details_text(details)
    assert "(не задано)" in text
    assert "(нет)" in text
    assert "(никто)" in text


async def _noop_fetch_all(repos: list[str]) -> list[RepoState]:
    return []


def _app(tmp_path: Path) -> GithubCheckerApp:
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r", "o/dst"]))
    return GithubCheckerApp(config_path)


@pytest.mark.anyio
async def test_p_opens_protection_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)

    async def fake_list(repo: str) -> list[RulesetInfo]:
        return [INFO]

    async def fake_get(repo: str, ruleset_id: int) -> RulesetDetails:
        return DETAILS

    monkeypatch.setattr(protection_module, "list_rulesets", fake_list)
    monkeypatch.setattr(protection_module, "get_ruleset", fake_get)

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        app.apply_states([RepoState(name="o/r", rulesets=[INFO])])
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, ProtectionScreen)
        table = app.screen.query_one(DataTable)
        assert table.row_count == 1


@pytest.mark.anyio
async def test_p_blocked_when_rulesets_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        app.apply_states([RepoState(name="o/r", rulesets=None)])
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        assert not isinstance(app.screen, ProtectionScreen)


@pytest.mark.anyio
async def test_toggle_enforcement_calls_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    calls: list[tuple[str, int, str]] = []
    infos = [INFO]

    async def fake_list(repo: str) -> list[RulesetInfo]:
        return list(infos)

    async def fake_get(repo: str, ruleset_id: int) -> RulesetDetails:
        return DETAILS

    async def fake_set(repo: str, ruleset_id: int, enforcement: str) -> None:
        calls.append((repo, ruleset_id, enforcement))
        infos[0] = infos[0].model_copy(update={"enforcement": enforcement})

    monkeypatch.setattr(protection_module, "list_rulesets", fake_list)
    monkeypatch.setattr(protection_module, "get_ruleset", fake_get)
    monkeypatch.setattr(protection_module, "set_ruleset_enforcement", fake_set)

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        app.apply_states([RepoState(name="o/r", rulesets=[INFO])])
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("e")
        await pilot.pause()
        assert calls == [("o/r", 1, "disabled")]
```

- [ ] **Step 2: Запустить — падает**

Run: `uv run pytest tests/test_protection.py -v`
Expected: ImportError — нет модуля `github_checker.protection`

- [ ] **Step 3: Реализация — `github_checker/protection.py`**

```python
"""Ruleset management screen."""

from collections.abc import Coroutine
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Footer, Header, Label, OptionList, Static

from github_checker.github import (
    GhError,
    copy_ruleset,
    delete_ruleset,
    get_ruleset,
    list_rulesets,
    set_ruleset_enforcement,
)
from github_checker.models import RulesetDetails, RulesetInfo

_RULE_LABELS = {
    "deletion": "запрет удаления",
    "non_fast_forward": "запрет force-push",
    "update": "запрет прямых пушей",
    "pull_request": "только через PR",
    "required_status_checks": "обязательные проверки CI",
    "required_signatures": "подписанные коммиты",
}


def protection_details_text(details: RulesetDetails) -> str:
    """Plain-text rendering of one ruleset for the details panel."""
    include = [
        "default" if ref == "~DEFAULT_BRANCH" else ref for ref in details.include
    ]
    lines = [details.name, f"enforcement: {details.enforcement}", ""]
    lines.append("Ветки: " + (", ".join(include) if include else "(не задано)"))
    if details.exclude:
        lines.append("Исключения: " + ", ".join(details.exclude))
    lines += ["", "Правила:"]
    if details.rules:
        lines += [f"  {_RULE_LABELS.get(r, r)}" for r in details.rules]
    else:
        lines.append("  (нет)")
    lines += ["", "Bypass:"]
    lines.append(
        "  " + ("; ".join(details.bypass) if details.bypass else "(никто)")
    )
    return "\n".join(lines)


class RepoPickerScreen(ModalScreen[str | None]):
    """Choose a target repository for a ruleset copy."""

    CSS = """
    RepoPickerScreen { align: center middle; }
    #dialog { width: 60; height: auto; border: thick $accent; padding: 1 2; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, repos: list[str]) -> None:
        super().__init__()
        self._repos = repos

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Копировать в репозиторий:")
            yield OptionList(*self._repos, id="repo-list")

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ) -> None:
        self.dismiss(str(event.option.prompt))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ProtectionScreen(Screen[list[RulesetInfo] | None]):
    """List, toggle, copy and delete rulesets of one repository."""

    CSS = """
    #rulesets { height: 1fr; }
    #protection-details-scroll { height: 1fr; border-top: solid $accent; padding: 0 1; }
    """
    BINDINGS = [
        ("e", "toggle", "Вкл/выкл"),
        ("c", "copy", "Копировать"),
        ("x", "delete", "Удалить"),
        ("escape", "close", "Назад"),
        ("q", "close", "Назад"),
    ]

    def __init__(self, repo: str, all_repos: list[str]) -> None:
        super().__init__()
        self._repo = repo
        self._other_repos = [r for r in all_repos if r != repo]
        self._infos: list[RulesetInfo] = []
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="rulesets", cursor_type="row")
        with VerticalScroll(id="protection-details-scroll"):
            yield Static("", id="protection-details", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self._repo
        table = self.query_one(DataTable)
        table.add_columns("Name", "Enforcement", "Target")
        self.run_worker(self._reload(), exclusive=True)

    async def _reload(self) -> None:
        try:
            self._infos = await list_rulesets(self._repo)
        except GhError as err:
            self.notify(_one_line(err.message), severity="error")
            return
        table = self.query_one(DataTable)
        table.clear()
        for info in self._infos:
            table.add_row(
                info.name, info.enforcement, info.target, key=str(info.id)
            )

    def _selected_info(self) -> RulesetInfo | None:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        row = table.cursor_coordinate.row
        if 0 <= row < len(self._infos):
            return self._infos[row]
        return None

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        info = self._selected_info()
        if info is not None:
            self.run_worker(self._load_details(info.id), exclusive=True)

    async def _load_details(self, ruleset_id: int) -> None:
        panel = self.query_one("#protection-details", Static)
        try:
            details = await get_ruleset(self._repo, ruleset_id)
        except GhError as err:
            panel.update(f"Не удалось загрузить детали: {_one_line(err.message)}")
            return
        panel.update(protection_details_text(details))

    def _run_op(self, coro: Coroutine[Any, Any, None]) -> None:
        if self._busy:
            self.notify("Операция уже выполняется", severity="warning")
            coro.close()
            return
        self.run_worker(self._guarded(coro))

    async def _guarded(self, coro: Coroutine[Any, Any, None]) -> None:
        self._busy = True
        try:
            await coro
        except GhError as err:
            self.notify(_one_line(err.message), severity="error")
        finally:
            self._busy = False

    def action_toggle(self) -> None:
        info = self._selected_info()
        if info is None:
            return

        async def op() -> None:
            new = "disabled" if info.enforcement == "active" else "active"
            await set_ruleset_enforcement(self._repo, info.id, new)
            self.notify(f"{info.name}: {new}")
            await self._reload()

        self._run_op(op())

    def action_copy(self) -> None:
        info = self._selected_info()
        if info is None or not self._other_repos:
            return

        def handle_result(target: str | None) -> None:
            if not target:
                return

            async def op() -> None:
                await copy_ruleset(self._repo, info.id, target)
                self.notify(f"{info.name} скопирован в {target}")

            self._run_op(op())

        self.app.push_screen(RepoPickerScreen(self._other_repos), handle_result)

    def action_delete(self) -> None:
        info = self._selected_info()
        if info is None:
            return
        from github_checker.app import ConfirmScreen

        def handle_result(confirmed: bool | None) -> None:
            if not confirmed:
                return

            async def op() -> None:
                await delete_ruleset(self._repo, info.id)
                self.notify(f"{info.name} удалён")
                await self._reload()

            self._run_op(op())

        self.app.push_screen(
            ConfirmScreen(f"Удалить ruleset «{info.name}»?"), handle_result
        )

    def action_close(self) -> None:
        self.dismiss(self._infos)


def _one_line(text: str) -> str:
    return " ".join(text.split())[:120]
```

Примечание: локальный импорт `ConfirmScreen` внутри `action_delete` — намеренно, чтобы избежать циклического импорта `app.py ↔ protection.py` (app импортирует ProtectionScreen на уровне модуля, protection импортирует ConfirmScreen лениво).

В `github_checker/app.py`:

1. Импорт: `from github_checker.protection import ProtectionScreen` и
   `RulesetInfo` уже импортирован в Task 4.
2. В BINDINGS после `("d", ...)`:

```python
        ("p", "protection", "Rulesets"),
```

3. Метод после `action_remove_repo`:

```python
    def action_protection(self) -> None:
        name = self._selected
        if name is None:
            return
        state = self._states.get(name)
        if state is None or state.error is not None:
            self.notify("Репозиторий в состоянии ошибки", severity="warning")
            return
        if state.rulesets is None:
            self.notify(
                "Нет данных о rulesets (нет прав или ошибка)", severity="warning"
            )
            return

        def handle_result(rulesets: list[RulesetInfo] | None) -> None:
            current = self._states.get(name)
            if current is None or rulesets is None:
                return
            current.rulesets = rulesets
            self.apply_states(list(self._states.values()))

        self.push_screen(ProtectionScreen(name, self._config.repos), handle_result)
```

- [ ] **Step 4: Тесты зелёные, линт, коммит**

```bash
uv run pytest tests/test_protection.py -v       # Expected: 5 passed
uv run pytest -q                                # Expected: 54 passed
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat: protection screen for ruleset management" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RHf1sQf1ekSY5MfEkbM2L3"
```

- [ ] **Step 5: Обновить README (клавиши)**

В таблицу клавиш `README.md` добавить строку:

```markdown
| `p` | rulesets выбранной репы (вкл/выкл, копировать, удалить) |
```

```bash
git add README.md
git commit -m "docs: document p key for ruleset management" \
  -m "Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01RHf1sQf1ekSY5MfEkbM2L3"
```

- [ ] **Step 6: Ручная smoke-проверка (интерактивно)**

Run: `uv run github-checker` — колонка Rules заполнена (`✓1` у atp-platform, `off1`/`-` у остальных), `p` на atp-platform открывает экран, детали показывают правила и bypass. Write-операции руками НЕ проверять на боевых репах без нужды.
Если терминал недоступен исполнителю — пометить как «требует ручной проверки пользователем».
