# Задание локального пути к репозиторию из TUI

**Дата:** 2026-07-07
**Статус:** одобрено к реализации

## Задача

Сейчас путь до локального клона (`RepoRef.path`) задаётся только правкой
`repos.toml`. Через TUI добавляется лишь имя (`add_repo` создаёт
`RepoRef(name=...)` с `path=None`). Нужно дать возможность задавать, менять и
очищать путь из интерфейса горячей клавишей.

## Решение

Отдельная горячая клавиша `l` открывает диалог с полем пути для **выбранной в
таблице** репы. Работает и для первичного задания, и для смены/очистки пути.
Путь перед сохранением валидируется как git-клон.

## Компоненты

### `localgit.py` — валидация

Новая функция:

```python
def is_git_repo(path: Path) -> bool:
    """True if *path* exists and is a git work tree (clone/worktree/submodule)."""
```

Реализация: `path.exists()` и успешный `git -C path rev-parse --git-dir`
(через существующий `_git`, ловя `LocalGitError`). Покрывает и обычные клоны, и
worktree/submodule, где `.git` — файл, а не каталог.

### `config.py` — запись пути

Новая функция:

```python
def set_path(path: Path, name: str, repo_path: Path | None) -> Config:
    """Set or clear the local clone path of *name*; save and return config."""
```

Находит `RepoRef` по имени, ставит `repo_path` (или `None` — очистка),
сохраняет через `save_config`, возвращает обновлённый `Config`. Имя, которого
нет в списке, — no-op (возвращает конфиг как есть).

### `app.py` — UI

**`SetPathScreen(ModalScreen[str | None])`** — по образцу `AddRepoScreen`:
поле `Input`, предзаполненное текущим путём выбранной репы (пустое, если пути
нет), кнопки OK/Cancel. Три исхода:

- **Cancel** → `dismiss(None)` — без изменений;
- **OK с пустым полем** → `dismiss("")` — очистить путь;
- **OK с текстом** → `dismiss(value)` — задать путь.

**Биндинг** `("l", "set_path", "Set path")` и `action_set_path`:

1. нет выбранной репы → ничего;
2. `push_screen(SetPathScreen(current), handle_result)`;
3. в `handle_result(result)`:
   - `None` → выход (Cancel);
   - `""` → `set_path(config_path, name, None)`, `action_refresh()`;
   - иначе → `p = Path(result).expanduser()`; если не `is_git_repo(p)` →
     `notify(..., severity="error")`, не сохраняем; иначе
     `set_path(config_path, name, p)`, `action_refresh()`.

`action_refresh()` перечитывает конфиг и рефетчит — локальный статус
подтянется автоматически.

## Обработка ошибок

- Не-git путь → уведомление об ошибке, путь не сохраняется.
- `~` в пути раскрывается через `Path.expanduser()`.
- Отсутствие выбранной репы → тихий выход (как у других действий).

## Тестирование

- `test_localgit`: `is_git_repo` — временный `git init` → True; обычный
  каталог → False; несуществующий путь → False.
- `test_config`: `set_path` — задать путь, сменить на другой, очистить (`None`);
  проверить персист в TOML и no-op для неизвестного имени.

## Документация

Строка в таблице клавиш README: `l` — задать/очистить путь к локальному клону.
