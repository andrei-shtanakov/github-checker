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

### Merge-gate-глаголы

    uv run github-checker pr-detail <dir> <pr>              # прочитать PR: состояние, чеки, диф
    uv run github-checker merge <dir> <pr> --if-head <sha>  # squash-merge за воротами гейта
    uv run github-checker post-merge-sync <dir>             # дефолт-бренч, ff-pull, прунинг

`pr-detail` — это *view*: `gh pr view` + review-треды по GraphQL (до
`MAX_THREAD_PAGES=5` страниц по `THREAD_PAGE_SIZE=100`) + `gh pr diff`, сведённые
в один `PrDetail`. `merge` — независимая точка проверки: перед мержем он сам
заново читает PR через `pr_detail()` и заново вычисляет все девять предикатов
гейта — `open`, `not-draft`, `mergeable`, `checks-green`, `checks-complete`,
`approvals`, `threads-resolved`, `threads-complete`, `squash-allowed` — плюс
собственную проверку `--if-head` (расхождение с текущим `headRefOid` PR — тоже
отказ). Устаревший или подменённый payload не может открыть ворота: гейт всегда
судит свежее состояние, а не то, что видел вызывающий.

`approvals` — allowlist, не blocklist: проходит только `reviewDecision = None`
(ревью не требуется репозиторием) или `APPROVED`; любое другое значение —
включая ещё не описанное будущее значение GitHub'овского enum — блокирует.
`threads-complete` и `checks-complete` — предикаты полноты чтения: усечённый
список нельзя путать с «открытых тредов нет» / «все чеки зелёные». Треды
усекает наш собственный лимит страниц; чеки усекает сам `gh` (`contexts` —
не более 100, и в `--json`-проекции нет никакого признака следующей страницы,
так что единственный доступный сигнал — что список пришёл ровно в потолок).
Ровно 100 зелёных чеков поэтому тоже отказ — редкий ложный отказ в безопасную
сторону. `mergeable = UNKNOWN` тоже блокирует — предикат `mergeable` требует
ровно значения `MERGEABLE`.

`mergeStateStatus` читается и показывается в `pr-detail`, но гейт его
намеренно не спрашивает: он блокировал бы ещё и `BEHIND`/`UNSTABLE`, а это
уже вопрос merge-политики владельца репозитория, а не корректности гейта.

Отказ гейта возвращает `pr_detail` целиком (чеки, треды, `gate_failed`) для
диагностики, но перед печатью в CLI из него стирается `diff` — отказ отвечает
на «почему», а не раздаёт содержимое PR повторно; за самим диффом — `pr-detail`.
Это только печать в `main.py`: `merge_pr()` как библиотечная функция по-прежнему
возвращает `PrDetail` целиком, включая `diff`.

`post-merge-sync` не разрушает ничего: грязное дерево (включая untracked),
detached HEAD, отсутствующий upstream, нерешаемый дефолтный бренч и дефолтный
бренч, занятый другим worktree, — всё это отказы; ни stash, ни reset, ни
force-switch, ни force-delete не используются. Смерженные локальные ветки
удаляются `git branch -d` (никогда `-D`). Если локального клона нет вовсе —
результат `ok=true, local_sync="not_applicable"` (удалённый мерж уже состоялся,
это не ошибка). Если список смерженных веток прочитать не удалось — синк всё
равно репортит успех с пометкой в `detail`: сама синхронизация уже прошла,
чистка веток к её контракту не относится.

Все шесть headless-глаголов (`pull`, `open-pr`, `propose-pr`, `pr-detail`,
`merge`, `post-merge-sync`) печатают ровно один JSON `ActionResult` и выходят
с кодом 1 при `ok=false`; непойманное исключение превращается в
`ActionResult(ok=false)` вместо traceback'а на stdout — сам traceback уходит в
stderr. `snapshot` и интерактивный TUI в этот контракт не входят — они
`ActionResult` не печатают.

Большие PR усекаются явно, не молча: `files_truncated`, `diff_truncated`,
`threads_truncated` и `checks_truncated` в payload. На гейт влияют
`threads_truncated` (предикат `threads-complete`) и `checks_truncated`
(предикат `checks-complete`) — эти два списка гейт читает, поэтому неполнота
в них меняет вердикт. `files_truncated`/`diff_truncated` только для
отображения: файлы и диф гейт не читает вовсе.

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
