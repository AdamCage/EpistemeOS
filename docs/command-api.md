# Локальные команды v1

Дата: 21 сентября 2026. `CommandService` и CLI `command` добавляют идемпотентную доставку к локальному Planning/Kernel/Search/Execution/Batch/PaperBuilder. Это запись перехода состояния; исполнение процесса, LLM-вызов и публикация не входят в handler. [ADR 0001](decisions/0001-command-admission.md) описывает транзакционную границу, [transport schema](../schemas/command-v1.schema.json) — оболочку запроса.

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
| `batch.plan` | planner | Frozen roster primary/reanalysis, назначенные actors, recipe и резерв `enqueued_attempt` |
| `batch.enqueue_slot` | Назначенный executor / replicator | Atomic run + job + уникальная slot binding |
| `batch.settle` | Автор batch, planner | Atomic полный batch settlement + search terminal; claim/review не создаются |
| `execution.enqueue` | executor / replicator | Atomic новый run + frozen job, занятый protocol attempt slot |
| `execution.dispatch` | Назначенный executor / replicator | Durable намерение однократного запуска; handler не запускает процесс |
| `execution.finalize` | Назначенный executor / replicator | Atomic проверенные result + execution_finalized |
| `planning.question`, `planning.explanation_set` | planner | Immutable версия вопроса/набора с prior refs и revision reason |
| `kernel.preregister_for_set` | planner | Protocol, привязанный к текущему question/set; hypotheses и scope выводятся из них |
| `kernel.hypothesis`, `kernel.preregister` | planner | Event ID |
| `kernel.start_run` | executor для primary; replicator при `replicate_of` | Run ID; процесс ещё не запущен |
| `kernel.finish_run` | Назначенный executor/replicator | Result ID |
| `kernel.claim` | analyst, executor | Proposed claim ID |
| `kernel.review` | reviewer | Review ID на immutable basis |
| `kernel.link_claims` | planner, analyst | Immutable proposal связи двух claims на ожидаемых bases |
| `kernel.review_with_links` | reviewer | Review v2 с явной оценкой каждой связи в evidence context |
| `kernel.expose_data` | planner, executor, replicator, analyst, reviewer | ID заявленного просмотра bytes |
| `search.register_tournament`, `search.register_tree`, `search.add_node`, `search.finish_selection` | planner | Event ID |
| `search.ballot` | judge, reviewer | Ballot ID; приоритет, не истинность |
| `search.select_next` | planner | Сохранённое решение с frontier/reservation |
| `paper.build` | writer | ID внутреннего draft; без materialization |

Blob должен быть заранее сохранён через `Store.put`/`put_json`; command ссылается на digest. Здесь нет универсальной загрузки файлов из произвольных agent paths. `paper.build` сохраняет bounded внутренние artifacts; `PaperBuilder.materialize` вызывается отдельно и заново проверяет актуальность evidence.

Прямые Python-методы и прежние CLI `review`/`paper` сохраняют optimistic concurrency, но не получают command idempotency автоматически. Новые retryable worker interfaces должны использовать `CommandService`, сохранять request и обрабатывать исторический acknowledgement отдельно от текущего workflow.

## Локальное исполнение

`execution.enqueue` требует `protocol`, `seed`, `outputs` (logical name → portable basename, обязательно `raw_data` и `metrics`), `wall_seconds` (1–86400), `max_output_bytes` (1–1 GiB на каждый output и каждый stdout/stderr). Optional `implementation`, `environment`, `replicate_of`, `required_capabilities`. Primary берёт source/environment из protocol; reanalysis требует другую implementation и получает ровно raw data исходного completed run. Environment создаётся `freeze_environment(store)` либо `episteme execution environment --root <directory>` и затем используется при preregistration.

Job фиксирует один Python source artifact. Программа вызывается как `python -I -S program.py input.dat --seed <seed>` в отдельном каталоге и записывает заявленные outputs. Это профиль для standard-library Python programs; multi-file source, dependencies/container reconstruction и DomainPack metrics ещё требуют расширения.

Capabilities: `separate_cwd`, `python_isolated_mode`, `bounded_output_capture`, а также `job_object_timeout` на Windows или `process_group_timeout` на POSIX. Неподдерживаемые требования, в том числе network isolation, отвергаются при enqueue. Capture cap не является OS disk quota; POSIX descendants должны оставаться в process group. `wall_seconds` отсчитывается после выдачи payload permit; подготовка supervisor и финальный capture входят в observed `elapsed_seconds`, но не в этот лимит. Supervisor должен быть жив для enforcement; Windows Job Object также закрывает назначенные процессы при смерти supervisor.

Обычный controller entry point — `episteme execution work <job-id> --root <directory>`. Он сначала сохраняет dispatch, затем выполняет worker вне SQL transaction. `execution status` читает queued/unknown/terminal состояние; `execution reconcile` проверяет существующую completion и сохраняет результат без запуска. После dispatch без доказанного завершения status остаётся `unknown`, даже если процесс всё ещё работает. CLI status/work/reconcile возвращает JSON состояния с code 0; failed/unknown job не следует считать successful experiment по exit code CLI. Ошибки команды дают code 2.

Прямой `kernel.finish_run` запрещён для managed job. Replay dispatch receipt не разрешает новый subprocess. Backup сохраняет завершённую provenance в CAS, но не активные workspaces; restored unknown job остаётся unknown. Подробности и crash windows: [ADR 0005](decisions/0005-local-runner.md). Исполняемый [пример](../examples/local_execution.py) не фабрикует scientific review.

## Версии планирования

`planning.question` принимает `study_id`, `statement`, `objective`, `scope`, непустые списки `constraints` и `stopping_criteria`; optional `parent` и `revision_reason` по умолчанию null. `planning.explanation_set` принимает `question`, минимум два existing `hypotheses`, `comparison_plan`; optional `parent`, `revision_reason`, `excluded_reasons`. Revision требует причину и актуальную parent head; removed candidates перечисляются в `excluded_reasons` ровно по одному с причиной. Root не содержит revision reason или исключений.

`kernel.preregister_for_set` принимает `explanation_set` и остальные параметры `kernel.preregister`, кроме `hypotheses` и `scope`: они выводятся из выбранных версий. Binding сохраняется внутри protocol event. Новый protocol не может использовать устаревший набор/вопрос; существующий protocol продолжает ссылаться на свою исходную версию. Его planning-bound amendment не может сбросить binding, перейти в другой study или другую question lineage. Полный контракт: [ADR 0004](decisions/0004-planning-lineage.md).

Для новых записей `context.study_id` должен совпадать с bound study. Та же проверка действует при последующих commands с bound protocol/run/claim/review/paper и при работе с tree, содержащим bound protocols. Она выполняется внутри admission transaction после проверки исторического replay. Legacy records без binding остаются без неявного study; различимость объяснений, исполнение ограничений и аутентификация этим не обеспечиваются.

## Связи claims и review v2

`kernel.link_claims` принимает `source`, `target`, `relation`, `rationale` и `expected_bases` с ровно двумя endpoint IDs. Relations: `supports`, `contradicts`, `limits`, `supersedes`. Scope должен совпадать точно. Review context включает входящие supports/limits ancestors и обе стороны contradiction/supersession; связь не устанавливает научную истинность. [ADR 0003](decisions/0003-claim-relations.md) описывает gate, replacement и ограничения.

Для такого context обычный `kernel.review` отказывает: нужен `kernel.review_with_links`. Его payload сохраняет прежние `claim`, `verdict`, `rationale`, `actions`, `expected_basis` и добавляет обязательный `link_assessments`:

```json
{
  "claim-link-id": {
    "judgment": "unresolved",
    "disposition": "needs_evidence",
    "rationale": "Нужно проверить альтернативное объяснение по исходным данным.",
    "evidence": ["source-claim-id", "target-claim-id"]
  }
}
```

Ключи должны покрывать ровно все context links. `judgment`: accepted/rejected/unresolved; `disposition`: compatible_as_written/requires_claim_revision/needs_evidence. Evidence — существующие event IDs из рассматриваемого контекста. Unresolved либо incompatible assessment запрещает `approve`. Принятое противоречие остаётся видимым даже при явно обоснованной совместимости с ограниченным выводом. Отсутствие replication не превращается в qualified source; rejected источник сохраняет свой настоящий mechanical status. [Persisted review schema](../schemas/review-with-links-v2.schema.json) описывает event payload, где `expected_basis` записывается как `basis_hash`.

Для accepted supports/limits/contradicts source должен проходить локальный gate на момент создания своего claim. В принятой цепочке supersession текущий gate требуется от источников, которые ещё не заменены другой accepted supersedes-связью в этом assessment. Внутренние звенья проверяются исторически: последовательные версии одного protocol могут сохранять правомерную историю при росте evidence. Все текущие bytes связанных попыток, включая failures, по-прежнему обязательны; новая версия проходит собственный текущий gate и отдельный review.

Новый action не меняет signature/defaults прежнего `kernel.review`: сохранённые v1 command receipts продолжают replay исходного ID. Старое review после добавления связи становится историческим и не покрывает новый context.

`gate.open_context_reviews` содержит открытые отрицательные reviews связанных claims. Для `approve` каждый такой ID должен быть явно указан в `evidence` хотя бы одной оценки связи; rationale объясняет совместимость с текущим ограниченным выводом. Это acknowledgement не закрывает исходное veto связанного claim. Его отрицательный verdict сохраняется между evidence revisions и меняется только новым approval того же reviewer. Эпизоды отрицательного review и первое закрывающее решение входят в зависимый basis; повторные обычные approvals не создают бесконечной взаимной инвалидации. Проверка ссылок не доказывает достаточность научного ответа: per-finding obligations и evidence-backed closure остаются в M4.

## Совместимость и восстановление

Writable opening аддитивно создаёт таблицу квитанций и triggers; прежние v1 events, IDs и hashes сохраняются. Read-only opening старой базы не мигрирует её. Полные квитанции можно прочитать через `receipts`/`Store.receipts()` и экспортировать через `Store.export_receipts()`; они содержат нормализованный request, результат и точные связи с event range.

Обычный `events.jsonl` и review bundle **не восстанавливают историю доставки**. Для возобновления без утраты idempotency реализованы CLI `backup`/`restore` и [directory snapshot v1](recovery.md): SQLite online backup вместе с проверенными CAS artifacts, без пересоздания events или receipts. Нельзя копировать только активный `state.sqlite3`, игнорируя WAL. Восстановление требует нового каталога и поддерживаемой схемы; общий migration framework ещё не реализован. Отдельные JSONL exports events/receipts следует делать при остановленном writer, если требуется один общий snapshot.

Изменение signature, default или типа action требует новой версии command API с сохранением обработки прежних v1 запросов. Добавлять поля в старый нормализатор незаметно для caller нельзя: это изменит fingerprint при replay. Текущая версия реализует только v1.

Транзакция обеспечивает однократный commit событий при повторной доставке. Внешний запуск, API charge и файловое materialization требуют отдельного outbox/lease протокола M2. При rollback могут остаться неиспользуемые CAS blobs; частичные events и receipt не фиксируются. Actor IDs остаются заявлениями доверенного caller, а hashes/triggers не защищают от владельца всей базы.
