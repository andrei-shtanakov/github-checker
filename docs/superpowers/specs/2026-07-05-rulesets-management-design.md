# github-checker — колонка защиты веток и управление rulesets

**Дата:** 2026-07-05
**Статус:** утверждено
**База:** расширение утилиты из `2026-07-05-github-checker-tui-design.md`

## Цель

Показывать в дашборде состояние защиты веток (GitHub rulesets) по каждой репе
и дать базовое управление: просмотр деталей, включение/выключение,
копирование ruleset в другую репу, удаление.

## Решения, принятые при обсуждении

- Объём правки: тумблер active/disabled, копирование, удаление.
  Редактирование состава правил и bypass-акторов — вне рамок
  (делается в вебе GitHub, тиражируется копированием).
- UI: в главной таблице только колонка-индикатор; управление — на отдельном
  экране по клавише `p` для выбранной репы.
- Данные: список rulesetов тянется в общем refresh (один дешёвый вызов на
  репу); детали ruleset — лениво при открытии экрана.
- Классическая branch protection не поддерживается (не используется
  в отслеживаемых репах); rulesets уровня организации — вне рамок.

## Модели (`models.py`)

```python
class RulesetInfo(BaseModel):
    """Item of GET repos/{r}/rulesets."""
    id: int
    name: str
    enforcement: str          # "active" | "disabled" | "evaluate"
    target: str               # "branch" | "tag" | ...

class RulesetDetails(BaseModel):
    """GET repos/{r}/rulesets/{id} — поля, нужные экрану."""
    id: int
    name: str
    enforcement: str
    target: str
    include: list[str]        # conditions.ref_name.include
    exclude: list[str]        # conditions.ref_name.exclude
    rules: list[str]          # [.rules[].type]
    bypass: list[str]         # человекочитаемые строки акторов
```

`RepoState` дополняется полем `rulesets: list[RulesetInfo] | None = None`
(`None` = не удалось получить: нет прав / ошибка; `[]` = rulesetов нет).

## Слой данных (`github.py`)

- В `fetch_repo` добавляется вызов `repos/{name}/rulesets?per_page=100`
  через тот же семафор. Ошибка этого вызова (в т.ч. 403/404) не считается
  ошибкой репы: `rulesets = None`.
- Новые функции (все асинхронные, через `gh api`, JSON-тела через stdin
  `--input -` либо `-f`/`-F` поля):

```python
async def get_ruleset(repo: str, ruleset_id: int) -> RulesetDetails
async def set_ruleset_enforcement(
    repo: str, ruleset_id: int, enforcement: str
) -> None                      # PUT repos/{repo}/rulesets/{id}
async def copy_ruleset(
    src_repo: str, ruleset_id: int, dst_repo: str
) -> None                      # GET полного тела + POST в dst
async def delete_ruleset(repo: str, ruleset_id: int) -> None
async def list_rulesets(repo: str) -> list[RulesetInfo]
```

- `_gh_api` расширяется параметрами `method: str = "GET"` и
  `body: dict | None = None` (сериализуется в JSON, передаётся через
  `--input -`; для DELETE допустим пустой ответ — не парсить JSON из
  пустого stdout).
- Копирование: из полного тела исходного ruleset удаляются служебные поля
  (`id`, `source`, `source_type`, `created_at`, `updated_at`,
  `current_user_can_bypass`, `node_id`, `_links`), остальное
  (`name`, `target`, `enforcement`, `conditions`, `rules`, `bypass_actors`)
  POSTится в целевую репу. Если POST вернул ошибку «name already taken»
  (HTTP 422) — повторить с именем `{name} (copy)`; вторая неудача —
  показать ошибку пользователю.
- Человекочитаемые bypass-акторы: `RepositoryRole id=5` → `admin (role)`,
  прочие роли — `role id={id}`, `Integration` → `app id={id}`,
  `Team` → `team id={id}`, `OrganizationAdmin` → `org admin`;
  режим добавляется суффиксом, например `admin (role), always`.

## UI

### Колонка `Rules` в главной таблице

Между `Alerts` и `Copilot`: `Repo | PRs | Bot | Branches | Alerts | Rules |
Copilot | Updated`.

| Значение | Условие |
|---|---|
| `✓N` | N rulesetов с enforcement=active (N >= 1) |
| `offN` | активных нет, но есть N выключенных/evaluate |
| `-` | rulesets == [] |
| `?` | rulesets is None (нет прав/ошибка) |
| `-` (как счётчики) | строка репы в состоянии error |

### Экран ProtectionScreen (клавиша `p`)

Открывается для выбранной репы; если репа в состоянии error или
`rulesets is None` — экран не открывается, показывается notify с причиной.

- Верх: таблица rulesetов `Name | Enforcement | Target`.
- Низ: панель деталей выбранного ruleset (ленивый `get_ruleset`):
  - ветки: include/exclude (`~DEFAULT_BRANCH` показывать как `default`);
  - правила по-русски: `deletion` → «запрет удаления»,
    `non_fast_forward` → «запрет force-push»,
    `update` → «запрет прямых пушей»,
    `pull_request` → «только через PR»,
    `required_status_checks` → «обязательные проверки CI»,
    `required_signatures` → «подписанные коммиты»,
    прочие типы — исходной строкой;
  - bypass-акторы (формат из слоя данных); пустой список — «(никто)».
- Клавиши:
  - `e` — переключить enforcement active↔disabled (evaluate трактуется
    как disabled при переключении в active);
  - `c` — модалка выбора целевой репы (список остальных реп из конфига,
    OptionList); после выбора — `copy_ruleset`;
  - `x` — удаление с ConfirmScreen;
  - `escape` или `q` — закрыть экран.
- После каждой write-операции: notify об успехе/ошибке + повторный
  `list_rulesets` этой репы, обновление таблицы экрана и `RepoState`
  в главном приложении (колонка `Rules` актуализируется без полного
  refresh).
- Write-операции запускаются воркером экрана; повторное нажатие клавиши
  до завершения операции игнорируется (флаг «операция в полёте»).

## Обработка ошибок

- Любая ошибка `gh api` в операциях экрана → notify severity="error"
  с текстом (обрезанным до одной строки), экран живёт дальше.
- 403 (нет admin-прав, ограничения тарифа для приватных реп) — тот же
  путь, отдельного кода не требуется.
- Ошибка загрузки деталей ruleset — сообщение в панели деталей вместо
  содержимого.

## Тесты

- Маппинг: фикстуры с реальной формы ответов `GET /rulesets` и
  `GET /rulesets/{id}` (включая bypass_actors из atp-platform) →
  `RulesetInfo`/`RulesetDetails`; ветка `rulesets=None` при GhError.
- Колонка: `repo_row` для случаев `✓1`, `off2`, `-`, `?`, error-строки.
- Копирование: сборка тела копии — служебные поля удалены, значимые
  сохранены; retry с `(copy)` при 422.
- `_gh_api` c method/body: аргументы субпроцесса собираются корректно
  (мок `create_subprocess_exec`), пустой stdout при DELETE не ломает.
- Экран: через `run_test` — открытие по `p`, рендер списка, `e` вызывает
  `set_ruleset_enforcement` (мок) и обновляет строку.

## Вне рамок (YAGNI)

- Редактирование состава правил, условий и bypass-акторов.
- Rulesets уровня организации; классическая branch protection.
- Массовое копирование в несколько реп за раз (копия — в одну репу;
  повторить при необходимости).
- История изменений, аудит.
