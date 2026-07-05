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
| `p` | rulesets выбранной репы (вкл/выкл, копировать, удалить) |
| `q` | выход |

Список реп хранится в `repos.toml` и правится либо из TUI, либо руками.
