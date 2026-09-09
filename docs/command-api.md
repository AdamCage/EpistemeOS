# Локальные команды v1

Дата: 9 сентября 2026. `CommandService` и CLI `command` добавляют идемпотентную доставку к локальному Kernel/Search/PaperBuilder. Это запись перехода состояния; исполнение процесса, LLM-вызов и публикация не входят в handler. [ADR 0001](decisions/0001-command-admission.md) описывает транзакционную границу, [transport schema](../schemas/command-v1.schema.json) — оболочку запроса.

## Использование

Сохраните в `command.json` пример из `examples` схемы. Для пустого research store он создаёт одну гипотезу:

```powershell
uv run episteme command --root .research/command-example --input command.json
uv run episteme command --root .research/command-example --input command.json
uv run episteme receipts --root .research/command-example
```

Оба первых вызова возвращают одинаковый event ID. Повтор после перезапуска либо других событий также возвращает первоначальный результат. Отдельный query `inspect`/`gate` показывает актуальное состояние. `meaning="historical_command_commit"` не означает актуального approval или успешного исполнения эксперимента.

В Python:

```python
from episteme.commands import CommandService, parse_command
from episteme.store import Store

with Store(".research/command-example") as store:
    result = CommandService(store).execute(parse_command(command_json_text))
```

`context.expected_revision` — число событий, прочитанных перед решением; для пустой истории `0`. `command_id` уникален во всём Store. Для повторной доставки после неопределённого ответа сохраняются исходные ID, revision и body. Изменённый запрос под тем же ID получает conflict. Новое решение после перечитывания истории получает новый ID. Пропущенные optional arguments и явно переданные значения по умолчанию нормализуются одинаково в v1.

`study_id` и `correlation_id` — обязательные непустые metadata. `causation_id` — `null` либо ID предшествующего immutable event; это ссылка на происхождение команды, не доказательство научной причинности. Эти поля не создают изоляцию studies или аутентификацию actors. Event envelope v1 не переписывается: metadata находятся в связанной квитанции.

## Действия

Имена полей `request.payload` совпадают с аргументами указанного метода без `self`. CommandService проверяет известные имена, обязательность, JSON-типы и роль до входа в обработчик. Domain validation повторяется внутри SQL-транзакции.

| Action | Допустимая роль | Результат |
|---|---|---|
| `kernel.hypothesis`, `kernel.preregister` | planner | Event ID |
| `kernel.start_run` | executor для primary; replicator при `replicate_of` | Run ID; процесс ещё не запущен |
| `kernel.finish_run` | Назначенный executor/replicator | Result ID |
| `kernel.claim` | analyst, executor | Proposed claim ID |
| `kernel.review` | reviewer | Review ID на immutable basis |
| `kernel.expose_data` | planner, executor, replicator, analyst, reviewer | ID заявленного просмотра bytes |
| `search.register_tournament`, `search.register_tree`, `search.add_node`, `search.finish_selection` | planner | Event ID |
| `search.ballot` | judge, reviewer | Ballot ID; приоритет, не истинность |
| `search.select_next` | planner | Сохранённое решение с frontier/reservation |
| `paper.build` | writer | ID внутреннего draft; без materialization |

Blob должен быть заранее сохранён через `Store.put`/`put_json`; command ссылается на digest. Здесь нет универсальной загрузки файлов из произвольных agent paths. `paper.build` сохраняет bounded внутренние artifacts; `PaperBuilder.materialize` вызывается отдельно и заново проверяет актуальность evidence.

Прямые Python-методы и прежние CLI `review`/`paper` сохраняют optimistic concurrency, но не получают command idempotency автоматически. Новые retryable worker interfaces должны использовать `CommandService`, сохранять request и обрабатывать исторический acknowledgement отдельно от текущего workflow.

## Совместимость и восстановление

Writable opening аддитивно создаёт таблицу квитанций и triggers; прежние v1 events, IDs и hashes сохраняются. Read-only opening старой базы не мигрирует её. Полные квитанции можно прочитать через `receipts`/`Store.receipts()` и экспортировать через `Store.export_receipts()`; они содержат нормализованный request, результат и точные связи с event range.

Обычный `events.jsonl` и review bundle **не восстанавливают историю доставки**. Для возобновления без утраты idempotency нужен согласованный backup SQLite вместе с CAS artifacts. Нельзя копировать только активный `state.sqlite3`, игнорируя WAL; используйте SQLite backup API или остановленный и checkpointed Store. Общий restore/import API пока не реализован. Отдельные JSONL exports events/receipts следует делать при остановленном writer, если требуется один общий snapshot.

Изменение signature, default или типа action требует новой версии command API с сохранением обработки прежних v1 запросов. Добавлять поля в старый нормализатор незаметно для caller нельзя: это изменит fingerprint при replay. Текущая версия реализует только v1.

Транзакция обеспечивает однократный commit событий при повторной доставке. Внешний запуск, API charge и файловое materialization требуют отдельного outbox/lease протокола M2. При rollback могут остаться неиспользуемые CAS blobs; частичные events и receipt не фиксируются. Actor IDs остаются заявлениями доверенного caller, а hashes/triggers не защищают от владельца всей базы.
