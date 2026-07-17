# github-checker

TUI-дашборд состояния нескольких GitHub-репозиториев: открытые PRы
(с пометкой dependabot), ветки, security alerts и статус Copilot-ревью.

## Требования

- Python 3.12+, [uv](https://docs.astral.sh/uv/)
- Авторизованный [gh CLI](https://cli.github.com) (`gh auth login`)

## Запуск

    uv run github-checker            # конфиг ~/.config/github-checker/repos.toml
    uv run github-checker --config path/to/repos.toml

## Headless-режим (для агентов и скриптов)

    uv run github-checker snapshot --workspace ..              # весь polyrepo-workspace
    uv run github-checker snapshot --workspace .. --local-only # без GitHub API

Обходит `<workspace>/*/.git` (конфиг не нужен) и печатает JSON: локальный
git-статус каждого репо (ветка, ahead/behind, dirty) плюс, если `gh`
авторизован, открытые PRы, issues, security alerts и rulesets. Без `gh`
деградирует до git-only и пишет причину в поле `gh_error`; поле `host`
помечает, чьи локальные клоны описаны. Потребители — скилл `fleet-check`
в `devtools/` и dispatcher (синк-коллектор).

### Headless-действия (белый список)

    uv run github-checker pull <dir>      # git pull --ff-only, JSON-результат
    uv run github-checker open-pr <dir>   # gh pr create --fill (или уже открытый PR), JSON

CLI-двойники TUI-клавиш `S`/`gh pr create` для программных потребителей
(dispatcher, действия по явному клику человека). `pull` только fast-forward —
диверженцию не трогает; `open-pr` идемпотентен (открытый PR репортится, не
дублируется) и **никогда не пушит** — незапушенная ветка это ошибка, не
side-effect. Выход — JSON `ActionResult`, exit 1 при неуспехе.

    uv run github-checker propose-pr <dir> --message "bump retries" \
        --edit project.yaml=/tmp/rendered.yaml \
        --if-match project.yaml=<sha256 исходного блоба>

Применяет явно переданное содержимое файлов (`--edit`, повторяемый) в
изолированном temp-worktree поверх `origin/<default>`, коммитит, пушит
свежую ветку и открывает PR через `gh pr create --fill`. `--if-match` —
опциональный guard от протухшей базы: sha256 сырых байт блоба на
`origin/<default>`, который видел вызывающий; при несовпадении команда
отказывает с ошибкой `"base file changed; reload required"`, ничего не
пушит. Если изменений относительно базы нет, результат `ok=false` с
`detail="no-op"` (структурный маркер, не ошибка выполнения). Инварианты:
всегда свежая ветка от актуального дефолтного бренча, никогда не force,
никогда не пушит в сам дефолтный бренч, файлы в живом working tree
вызывающего не читаются как источник контента и не изменяются.

### Snapshot-контракт v1 (заморожен)

Форма snapshot-JSON — версионируемый контракт: `contracts/snapshot/v1/`
(`snapshot.schema.json` + golden-фикстуры full/degraded). Выход несёт поле
`schema_version: 1`. Правила:

- потребители **вендорят пиненую копию** схемы к себе и проверяют
  `schema_version`;
- обратимо-совместимые добавления (новые optional-поля) остаются v1, но
  обязаны в том же PR осознанно обновить `snapshot.schema.json` — CI-тест
  (`tests/test_snapshot_contract.py`) требует точного совпадения модели с
  замороженным файлом, молчаливый drift невозможен;
- breaking-изменение — только как `contracts/snapshot/v2/` рядом с v1,
  никогда правкой v1.

## Клавиши

| Клавиша | Действие |
|---|---|
| `r` | обновить сейчас |
| `a` | добавить репозиторий |
| `d` | удалить выбранный |
| `l` | задать/очистить путь к локальному клону |
| `p` | rulesets выбранной репы (вкл/выкл, копировать, удалить) |
| `s` | fetch локального клона выбранной репы (безопасно) |
| `S` | pull локального клона (только fast-forward) |
| `q` | выход |

Список реп хранится в `~/.config/github-checker/repos.toml` и правится либо
из TUI, либо руками. Файл живёт вне репозитория, поэтому git-операции его
не трогают. При первом запуске старый `./repos.toml` (если есть) переносится
туда автоматически; образец — `repos.toml.example`. У репозитория можно
указать необязательное поле `path` с путём до локального клона — тогда в
деталях появится статус ahead/behind/dirty и станут доступны клавиши `s`/`S`.
