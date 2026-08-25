# snapshot contract — v2

Замороженный контракт headless-выхода `github-checker snapshot
--schema-version 2`. Потребители вендорят `snapshot.schema.json` +
`fixtures/` пиненой копией и ключуются по `schema_version`. Любое ломающее
изменение уходит в `v3` рядом, никогда — правкой v2 на месте; аддитивное
изменение осознанно обновляет схему в том же PR (остаётся v2).

**v1 остаётся опубликованным и остаётся дефолтом CLI** до миграции его
текущих потребителей: эмиссия v2 — явный opt-in флагом, не flag-day.

## Что v2 добавляет к v1

1. **Нормализованная классификация эпика** на каждом открытом `Issue` и
   `PullRequest` — поле `epic` с объектом по `classification.schema.json`
   контракта `epics/v1` (вендорён в `github_checker/contract_epics/`,
   см. Pin linkage ниже): `epic` / `defect` / `classification` /
   `diagnostics` / `subject_uri` / `carrier` / `observed_at`.
2. **Окно атрибуции смерженных PR** — секция `merged` на репозитории:
   `window_days`, `truncated` и по каждому смерженному в окне PR —
   `number`, `merge_commit_sha`, `commit_shas`, `commit_shas_truncated`,
   `merged_at` и та же классификация `epic`.

**Сырые тела issue/PR в снапшот не входят по построению**: лишний объём,
PII и поверхность prompt-injection. Разбор трейлеров происходит у
продюсера; наружу идёт только вердикт.

## Граница потребления (нормативная)

**dispatcher потребляет только открытые плоскости `issues` / `pulls`.
Окно `merged` — транспорт для robin (восстановление `commit → PR` без
эвристик: связь доказывается `commit_shas`/`merge_commit_sha`, а не
конвенцией `#123` в subject) и НЕ является состоянием.** Читать `merged`
как «состояние репозитория» — ошибка потребителя: это скользящее окно с
явным усечением, а не реестр.

## Семантика классификации

`classification` — закрытая четвёрка:

| state | значение |
|---|---|
| `tagged` | тело прочитано, несёт ровно один грамматически валидный `Epic:` |
| `missing` | тело прочитано, трейлера `Epic:` нет |
| `invalid` | тело прочитано, значение непригодно (грамматика, кратность) |
| `unavailable` | тело НЕ прочитано (не пришло в payload, плоскость недоступна) |

`unavailable` никогда не считается `missing` — и никогда не считается
`tagged`. Агрегат, складывающий полностью прочитанную плоскость с
непрочитанной, обязан нести пополосную полноту с собой.

**Граница слоя** (ADR-ECO-010): продюсер доказывает наличие и грамматику,
не больше. `tagged` здесь означает «валидный эпик присутствует»;
принадлежность реестру (EP-UNKNOWN, EP-MOVED) — вопрос потребителя:
реестр значений (`epics.toml` зонтика) читается живьём сенсором и не
читается продюсером. EP-MISSING эмитится с `severity: warning`;
эскалация по `missing_error_after` — тоже решение слоя, читающего реестр.

## Усечение — явное, никогда молчаливое

Каждый листинг ограничен одной страницей (100). Попадание в кап
трактуется как возможная неполнота и репортится, а не съедается:

- `merged.truncated: true` — страница закрытых PR упёрлась в кап и её
  самый старый по `updated` элемент всё ещё внутри окна: смерженные в
  окне PR могли остаться за страницей. Усечённое окно нельзя читать как
  пустое.
- `commit_shas_truncated: true` — список коммитов PR упёрся в кап.
- Тело, не пришедшее в payload листинга, — `classification: "unavailable"`,
  не пустое тело.

## Pin linkage (epics/v1)

Классификация формируется по контракту `epics/v1`; его пин у продюсера:

- источник: `prograph-vault authored/contracts/epics/v1`
- commit: `15cd338e01176fd126a7e8b8925c88ada8bface6`
- `tree_sha256`: `4bf65a4d4526e24b58f85d21a6c1eaaaca6dcff28803cb8f0306917c344c289e`

Копия и её fingerprint — `github_checker/contract_epics/` (PINNED.txt +
manifest.json); целостность проверяется в CI
(`tests/test_contract_epics_integrity.py`, guarantee A), конформанс —
реплеем pull_request/issue-фикстур контракта
(`tests/test_epics.py`).

## Файлы

| Файл | Роль |
|---|---|
| `snapshot.schema.json` | JSON Schema модели `WorkspaceSnapshotV2` (генерируется из pydantic-модели, равенство закреплено `tests/test_snapshot_v2_contract.py`) |
| `fixtures/snapshot_full.json` | golden: обе плоскости классифицированы; покрыты все четыре состояния (включая `unavailable`), оба состояния обоих флагов усечения, `merge_commit_sha: null` |
| `fixtures/snapshot_degraded.json` | golden: gh недоступен — только локальный git, `gh_error` заполнен |
