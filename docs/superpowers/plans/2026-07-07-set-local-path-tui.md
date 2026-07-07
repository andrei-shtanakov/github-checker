# Set Local Clone Path from TUI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать возможность задавать, менять и очищать путь к локальному клону репозитория горячей клавишей `l` прямо из TUI, а не только правкой `repos.toml`.

**Architecture:** Три слоя. `localgit.is_git_repo` валидирует путь как git work tree. `config.set_path` пишет/очищает `RepoRef.path` и сохраняет TOML. В `app.py` новый модальный экран `SetPathScreen` и действие `action_set_path` связывают их с клавишей `l`, после сохранения дёргается `action_refresh`.

**Tech Stack:** Python 3.12, Textual (TUI), Pydantic, pytest + anyio, uv.

## Global Constraints

- Package manager: `uv` только (`uv run pytest`, `uv add`). Никогда pip.
- Type hints обязательны для всего кода; docstrings у публичных функций.
- Line length: 88 символов максимум.
- Формат/проверки перед коммитом: `uv run ruff format .`, `uv run ruff check .`.
- Async-тесты через anyio (`@pytest.mark.anyio`), не asyncio.
- PEP 8: snake_case функции/переменные, PascalCase классы.
- Следовать существующим паттернам (`AddRepoScreen`, `add_repo`, `_init_repo` в тестах).

---

### Task 1: `localgit.is_git_repo` — валидация пути

**Files:**
- Modify: `github_checker/localgit.py` (добавить функцию после `local_status`)
- Test: `tests/test_localgit.py`

**Interfaces:**
- Consumes: существующие `_git`, `LocalGitError` из `localgit.py`.
- Produces: `def is_git_repo(path: Path) -> bool` — True, если `path` существует и является git work tree (обычный клон, worktree или submodule).

- [ ] **Step 1: Write the failing tests**

В `tests/test_localgit.py` добавить (в файле уже есть хелпер `_init_repo` и импорт `Path`, `pytest`):

```python
def test_is_git_repo_true_for_clone(tmp_path: Path) -> None:
    repo = tmp_path / "clone"
    _init_repo(repo)
    assert is_git_repo(repo) is True


def test_is_git_repo_false_for_plain_dir(tmp_path: Path) -> None:
    assert is_git_repo(tmp_path) is False


def test_is_git_repo_false_for_missing_path(tmp_path: Path) -> None:
    assert is_git_repo(tmp_path / "nope") is False
```

Добавить `is_git_repo` в импорт из `github_checker.localgit` вверху файла:

```python
from github_checker.localgit import (
    LocalGitError,
    fetch,
    is_git_repo,
    local_status,
    pull_ff_only,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_localgit.py -k is_git_repo -v`
Expected: FAIL (ImportError: cannot import name `is_git_repo`).

- [ ] **Step 3: Write minimal implementation**

В `github_checker/localgit.py` добавить после `local_status` (перед `fetch`):

```python
def is_git_repo(path: Path) -> bool:
    """True if *path* exists and is a git work tree (clone/worktree/submodule)."""
    if not path.exists():
        return False
    try:
        _git(path, "rev-parse", "--git-dir")
    except LocalGitError:
        return False
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_localgit.py -k is_git_repo -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Format, lint, commit**

```bash
uv run ruff format . && uv run ruff check .
git add github_checker/localgit.py tests/test_localgit.py
git commit -m "feat: localgit.is_git_repo validates a path as a git work tree

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `config.set_path` — запись/очистка пути

**Files:**
- Modify: `github_checker/config.py` (добавить функцию после `remove_repo`)
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: `load_config`, `save_config`, `Config`, `RepoRef` (уже в модуле).
- Produces: `def set_path(path: Path, name: str, repo_path: Path | None) -> Config` — находит `RepoRef` по `name`, ставит `repo_path` (или `None` для очистки), сохраняет, возвращает обновлённый `Config`. Неизвестное `name` — no-op, возвращает конфиг как есть.

- [ ] **Step 1: Write the failing tests**

В `tests/test_config.py` добавить `set_path` в импорт из `github_checker.config`, затем тесты:

```python
def test_set_path_sets_and_clears(tmp_path: Path) -> None:
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    clone = tmp_path / "clone"

    updated = set_path(config_path, "o/r", clone)
    assert updated.repos[0].path == clone
    assert load_config(config_path).repos[0].path == clone

    changed = tmp_path / "other"
    set_path(config_path, "o/r", changed)
    assert load_config(config_path).repos[0].path == changed

    set_path(config_path, "o/r", None)
    assert load_config(config_path).repos[0].path is None


def test_set_path_unknown_name_is_noop(tmp_path: Path) -> None:
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    updated = set_path(config_path, "o/missing", tmp_path / "x")
    assert [r.name for r in updated.repos] == ["o/r"]
    assert updated.repos[0].path is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -k set_path -v`
Expected: FAIL (ImportError: cannot import name `set_path`).

- [ ] **Step 3: Write minimal implementation**

В `github_checker/config.py` добавить после `remove_repo`:

```python
def set_path(path: Path, name: str, repo_path: Path | None) -> Config:
    """Set or clear the local clone path of *name*; save and return config."""
    config = load_config(path)
    updated = config.model_copy(
        update={
            "repos": [
                ref.model_copy(update={"path": repo_path})
                if ref.name == name
                else ref
                for ref in config.repos
            ]
        }
    )
    save_config(path, updated)
    return updated
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -k set_path -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Format, lint, commit**

```bash
uv run ruff format . && uv run ruff check .
git add github_checker/config.py tests/test_config.py
git commit -m "feat: config.set_path sets or clears a repo's local clone path

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `SetPathScreen` + `action_set_path` — UI и клавиша `l`

**Files:**
- Modify: `github_checker/app.py` (новый экран после `AddRepoScreen`; импорт `set_path`, `is_git_repo`; биндинг; действие)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `config.set_path`, `localgit.is_git_repo`, `RepoRef`, `self._selected`, `self._selected_ref()`, `self.action_refresh()`.
- Produces: клавиша `l` → `action_set_path`. `SetPathScreen(ModalScreen[str | None])` dismiss'ит `None` (Cancel), `""` (очистить путь) или строку пути (задать).

- [ ] **Step 1: Write the failing tests**

В `tests/test_app.py` добавить (в файле есть `_init_repo`? нет — используем `git init` через subprocess. Проще: для «валидного» пути замокать `is_git_repo`). Добавить тесты:

```python
@pytest.mark.anyio
async def test_set_path_writes_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    monkeypatch.setattr(app_module.localgit, "is_git_repo", lambda path: True)
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    app = GithubCheckerApp(config_path)
    clone = tmp_path / "clone"
    async with app.run_test() as pilot:
        app.apply_states([STATE.model_copy(update={"name": "o/r"})])
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        await pilot.press(*str(clone))
        await pilot.press("enter")
        await pilot.pause()
    from github_checker.config import load_config

    assert load_config(config_path).repos[0].path == clone


@pytest.mark.anyio
async def test_set_path_rejects_non_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    monkeypatch.setattr(app_module.localgit, "is_git_repo", lambda path: False)
    notes: list[str] = []
    config_path = tmp_path / "repos.toml"
    save_config(config_path, Config(repos=["o/r"]))
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        app.apply_states([STATE.model_copy(update={"name": "o/r"})])
        await pilot.pause()
        monkeypatch.setattr(app, "notify", lambda *a, **k: notes.append(a[0]))
        await pilot.press("l")
        await pilot.pause()
        await pilot.press(*str(tmp_path / "plain"))
        await pilot.press("enter")
        await pilot.pause()
    from github_checker.config import load_config

    assert load_config(config_path).repos[0].path is None
    assert any("git" in n for n in notes)


@pytest.mark.anyio
async def test_set_path_clears_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "fetch_all", _noop_fetch_all)
    config_path = tmp_path / "repos.toml"
    save_config(
        config_path,
        Config(repos=[RepoRef(name="o/r", path=tmp_path / "clone")]),
    )
    app = GithubCheckerApp(config_path)
    async with app.run_test() as pilot:
        app.apply_states([STATE.model_copy(update={"name": "o/r"})])
        await pilot.pause()
        await pilot.press("l")
        await pilot.pause()
        await pilot.click("#ok")
        await pilot.pause()
    from github_checker.config import load_config

    assert load_config(config_path).repos[0].path is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_app.py -k set_path -v`
Expected: FAIL (нет клавиши `l` / экран не появляется → путь не меняется, ассерты падают).

- [ ] **Step 3: Add the screen**

В `github_checker/app.py` после класса `AddRepoScreen` (перед `ConfirmScreen`) добавить:

```python
class SetPathScreen(ModalScreen[str | None]):
    """Prompt for a local clone path; empty means clear, cancel means no change."""

    CSS = """
    SetPathScreen { align: center middle; }
    #dialog { width: 70; height: auto; border: thick $accent; padding: 1 2; }
    #dialog Horizontal { height: auto; align-horizontal: right; }
    """

    def __init__(self, current: str) -> None:
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Путь к локальному клону (пусто — очистить):")
            yield Input(value=self._current, placeholder="/path/to/clone", id="path-input")
            with Horizontal():
                yield Button("Save", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ok":
            self.dismiss(self.query_one("#path-input", Input).value.strip())
        else:
            self.dismiss(None)
```

- [ ] **Step 4: Wire the binding, imports, and action**

В `github_checker/app.py`:

1. Обновить импорт из `config` (строка ~16):

```python
from github_checker.config import add_repo, load_config, remove_repo, set_path
```

2. Добавить биндинг в `BINDINGS` (после строки `("d", "remove_repo", "Remove repo"),`):

```python
        ("l", "set_path", "Set path"),
```

3. Добавить метод (после `action_remove_repo`):

```python
    def action_set_path(self) -> None:
        """Set, change, or clear the local clone path of the selected repo."""
        ref = self._selected_ref()
        if ref is None:
            return
        name = ref.name
        current = str(ref.path) if ref.path is not None else ""

        def handle_result(result: str | None) -> None:
            if result is None:
                return
            if result == "":
                self._config = set_path(self._config_path, name, None)
                self.action_refresh()
                return
            path = Path(result).expanduser()
            if not localgit.is_git_repo(path):
                self.notify(
                    f"Не git-репозиторий: {path}", severity="error"
                )
                return
            self._config = set_path(self._config_path, name, path)
            self.action_refresh()

        self.push_screen(SetPathScreen(current), handle_result)
```

(`localgit` и `Path` уже импортированы в модуле.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_app.py -k set_path -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest`
Expected: PASS (все тесты зелёные).

- [ ] **Step 7: Format, lint, commit**

```bash
uv run ruff format . && uv run ruff check .
git add github_checker/app.py tests/test_app.py
git commit -m "feat: 'l' hotkey sets/clears a repo's local clone path from the TUI

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Документация — клавиша `l` в README

**Files:**
- Modify: `README.md` (таблица клавиш)

**Interfaces:**
- Consumes: ничего. Produces: строка в таблице клавиш.

- [ ] **Step 1: Add the key row**

В `README.md`, в таблице «Клавиши», после строки `| \`d\` | удалить выбранный |` добавить:

```markdown
| `l` | задать/очистить путь к локальному клону |
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document 'l' hotkey for setting the local clone path

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:** `is_git_repo` (Task 1) ↔ спек §localgit; `set_path` (Task 2) ↔ §config; `SetPathScreen`+`action_set_path`+биндинг `l` (Task 3) ↔ §app.py и три исхода (Cancel/пусто/путь), валидация + expanduser + error notify; README (Task 4) ↔ §Документация. Тесты покрывают все ветки. Пробелов нет.
- **Placeholder scan:** плейсхолдеров нет; весь код показан.
- **Type consistency:** `set_path(path, name, repo_path: Path | None) -> Config` одинаково в Task 2 и вызовах Task 3; `is_git_repo(path: Path) -> bool` одинаково в Task 1 и Task 3; `SetPathScreen(ModalScreen[str | None])` с исходами `None`/`""`/str согласован с `handle_result`.
