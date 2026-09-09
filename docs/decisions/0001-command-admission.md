# ADR 0001: допуск команд и идемпотентная доставка

Статус: **принято и реализовано для локального M1**, 9 сентября 2026. Основания: [архитектура](../architecture.md), [MVP-план](../mvp-plan.md). Транзакционный контракт реализован в [Store](../../src/episteme/store.py), versioned dispatch — в [CommandService](../../src/episteme/commands.py). Проверены локальные storage/service/CLI сценарии; аутентификация, внешнее исполнение, межверсионный dispatcher и восстановление из JSONL ещё не реализованы.

## Проблема

`Kernel` и `Search` читают историю, проверяют переход и вызывают `Store.append(expected_revision=len(history))`. `append` начинает `BEGIN IMMEDIATE` и отклоняет устаревшего writer, но повтор уже принятой команды снова исполняет проверку и генерирует новый ID. `PaperBuilder.build` дополнительно сохраняет blobs до проверки роли и ревизии в `_write`. Повтор после потери ответа может создать второй переход либо получить «already terminal» вместо исходного результата.

Нужна общая граница допуска **до вызова обработчика**, сохраняющая оригинальные события v1 и их hashes. Она обеспечивает однократный commit логического перехода при повторной доставке, но не однократное выполнение внешнего процесса; последнее требует outbox/attempt/lease в M2.

## Минимальный API

```python
context = dict(
    command_id="caller-generated-stable-id",
    expected_revision=42,
    actor="planner-1",
    role="planner",
    study_id="study-1",
    correlation_id="cycle-1",
    causation_id=None,
)
request = dict(
    version=1,
    action="kernel.preregister",
    payload=normalized_arguments,
)
result = store.command(context, request, handler)
```

`handler()` — доверенный синхронный обработчик зарегистрированного action, возвращающий JSON-совместимый результат: например прежний ID либо словарь `Search.select_next`. Полученный результат сохраняется целиком; при повторе возвращается десериализованная копия. `Store.receipts()` возвращает проверенные квитанции, `Store.export_receipts()` — их JSONL. Python callable не поступает от внешнего агента: внешний локальный API — `CommandService(store).execute({"context": context, "request": request})`, выбирающий обработчик и разрешённые роли из явного реестра.

CommandService формирует полный request до выполнения текущего тела метода. В digest входят все аргументы, включая значения по умолчанию, actor, role, action, version и исходный `expected_revision`. Отдельный `command_id` уникален внутри БД. Изменённая ревизия с тем же ID — изменённая команда и conflict; повтор идентичного запроса допускается, даже если текущая ревизия выросла. Новая попытка после перепланирования получает новый ID. `study_id`, `correlation_id` и `causation_id` также сохраняются и входят в fingerprint; causation связывается с существующим предшествующим event ID, но не доказывает научную причинность. `study_id` пока metadata namespace: одна БД может содержать legacy events, и меж-study access isolation этот API не обеспечивает.

Минимальная совместимость: прямые вызовы Kernel/Search/PaperBuilder продолжают прежнюю запись с optimistic revision и **не получают** обещания идемпотентности. Публичная командная граница с context обязательна для будущих worker/provider interfaces. Transport `command_id`, event ID и run attempt ID обозначают разные сущности.

## Версия request и развитие API

Версия 1 впервые вводится вместе с текущими action signatures, defaults и проверками типов. CommandService сейчас использует `inspect.signature()` и annotations существующих методов для нормализации: пропущенный optional argument и явно переданное default дают один fingerprint. Статистические поля протокола уже входят в этот первоначальный v1 контракт.

После публикации изменение default, добавление optional argument, удаление поля либо изменение типа требует новой `request.version` с сохранением v1 normalizer и совместимого handler. Иначе прежний оригинальный envelope после обновления даст другой fingerprint либо будет отвергнут ещё до lookup receipt. Замороженный реестр версий и dispatcher совместимости пока **не реализованы**; до следующего изменения командного контракта нужен их отдельный тест на replay старого envelope. Неизвестная version сейчас отклоняется до handler, а не интерпретируется как v1.

## Порядок допуска

1. CommandService проверяет структуру envelope, зарегистрированный action, разрешённую роль, имена/типы аргументов и нормализует defaults; Store проверяет writable mode и строгую структуру context/request. Ревизия — неотрицательное целое, boolean запрещён; ID/actor/role/action — непустые строки. Payload — строгий JSON с конечными числами и строковыми ключами. Сериализация и копирование происходят до handler, исключая изменение caller-owned mutable arguments после вычисления digest. Доверенный handler не должен менять request snapshot или использовать отсутствующие в нём аргументы.
2. Начать короткий `BEGIN IMMEDIATE`; проверить существующую event chain и найденную квитанцию. При совпадающем command ID и отличающемся fingerprint вернуть conflict до handler. При полном совпадении проверить связи квитанции с событиями и вернуть сохранённый результат без повторного handler и проверки текущего `expected_revision`.
3. Если квитанции нет, сравнить ожидаемую ревизию с текущей длиной проверенной истории. Conflict не вызывает handler, не создаёт blobs, events или квитанцию. Ошибка блокировки SQLite не является допуском; caller перечитывает состояние перед новой командой, а при неопределённом ответе повторяет исходный ID/body.
4. Вызвать handler внутри той же транзакции. Семантические проверки ссылок, budget, role assignment, review basis и eligibility выполняются на этом состоянии. Handler может создать несколько последовательных событий; каждый `append` проверяет свою фактическую промежуточную ревизию. Actor/role событий обязаны совпадать с context. Прямые обращения к SQL, смена actor и отдельные внутренние commit запрещены контрактом обработчика.
5. Проверить JSON-результат и сохранить immutable receipt с точным диапазоном созданных событий. Для первого API принимаются переходы с хотя бы одним событием; read-only вычисления остаются query API. Одним commit зафиксировать события и receipt. Любое исключение отменяет всю транзакцию; ошибка внутреннего append делает команду rollback-only, даже если обработчик её перехватил.

Первичную проверку роли нельзя оставлять только в `_write`: иначе недопустимый writer уже создаст paper blobs. Контекст пока задаёт доверенный caller; это разделение обязанностей, **не аутентификация**. Будущий write-service проверяет реального principal и актуальное право получить receipt до выдачи сохранённого результата. Знание command ID не предоставляет полномочий. Fingerprint с actor/role предотвращает возврат чужой квитанции только в пределах этой модели доверия.

## Владение транзакцией

`Store.append` имеет внутреннюю ветку участия в **явно принадлежащей `Store.command` транзакции**. Такая ветка не выполняет `BEGIN`, `COMMIT` или самостоятельный `ROLLBACK`. Обычный legacy append сохраняет существующий режим владения транзакцией. Проверки одного `db.in_transaction` недостаточно: это могла быть чужая вручную открытая транзакция.

Вложенные `Store.command` запрещены в первом API. Составной обработчик вызывает внутренние методы переходов, а не повторно входит в публичную command-обёртку. Контекст владения очищается в `finally`, включая ошибки сериализации, вставки receipt и commit. Соединение Store используется одним потоком; конкурирующие writers имеют разные SQLite connections.

Долгие вычисления, LLM/API, запуск процессов, materialization экспортов и ожидание worker не входят в handler. Текущее небольшое построение paper может сформировать bounded immutable blobs внутри допуска; для больших bundles нужно отдельно спроектировать prepare/admit/finalize с проверкой snapshot. Нельзя механически оборачивать весь `demo` или afterlife scan в SQL write lock.

## Receipt и совместимость истории

Добавлена отдельная таблица `command_receipts(command_id, receipt, hash)` с уникальным command ID и append-only triggers. Canonical JSON receipt содержит schema version, command ID, context, request и его digest, before/after revision и head hash, ordered event IDs/hashes, result и creation timestamp. SHA-256 checksum связывает все эти поля. Перед повтором проверяются checksum, fingerprint, contiguous range, соответствие сохранённых event IDs/hashes проверенной chain и принадлежность событий actor/role. Диапазоны разных квитанций не перекрываются; legacy events между ними допустимы. `receipts()` читает обе таблицы в одной SQL read transaction, чтобы конкурентный commit не выглядел как повреждение диапазона.

Существующие строки `events`, `schema_version=1`, IDs, timestamps, payloads и event hashes **не изменяются**. Receipt metadata не добавляются задним числом в envelope событий. Обновление схемы таблиц аддитивно и идемпотентно; отсутствующая таблица в старой БД означает отсутствие квитанций, а не corruption. `read_only=True` открывает старую БД без migration или filesystem writes; command всегда отклоняется до handler, включая попытку replay. Неизвестная версия существующей receipt schema требует явной migration и не трактуется как пустая таблица.

Event log остаётся источником научного состояния; receipt ledger — дополнительный источник истории доставки. Текущий events-only JSONL сохраняет научные ссылки, но **не** гарантирует идемпотентное возобновление доставки. `export_receipts()` сохраняет отдельный ledger; согласованный backup БД включает обе таблицы. Полный JSONL restore/import с проверкой привязки ledger к chain ещё не реализован. Отдельно полученные exports могут относиться к разным snapshots, поэтому их нельзя объявлять готовым consistent backup без дополнительной проверки. Нельзя восстанавливать фиктивные command IDs для старых событий.

Checksums и triggers выявляют случайную порчу в доверенном локальном процессе. Владелец файлов может удалить историю вместе с receipt либо согласованно переписать обе таблицы; внешний checkpoint и write-service остаются отдельной задачей.

## Evidence и файловые эффекты

Receipt подтверждает исторический commit, не актуальную научную состоятельность. Повтор `review` или `paper.build` после нового evidence возвращает исходный ID и не создаёт новое approval. Следующий переход и `PaperBuilder.materialize` по-прежнему проверяют текущую eligibility и artifact bytes; старое receipt не заменяет gate. Replay не запускает materialization и не перепроверяет старый verdict как новый переход.

Blobs остаются content-addressed и сохраняются до ссылочного события. Ошибка handler/commit может оставить orphan blob, но не частично принятые events/receipt. Не удалять blobs при rollback: те же bytes мог сохранить другой writer. Stale request и duplicate replay не вызывают handler, поэтому сами не создают blobs. Garbage collection, если понадобится, должна учитывать все committed references и незавершённые подготовки.

## Проверки приёмки

Storage-проверки находятся в [test_commands_store.py](../../tests/test_commands_store.py): 23 теста прошли 9 сентября 2026. Интеграция Kernel/Search/PaperBuilder/CLI проверяется в [test_commands_service.py](../../tests/test_commands_service.py). Полный suite на промежуточном состоянии shared worktree прошёл 164 теста с одним Windows symlink skip; итоговый прогон после объединения изменений ведётся в [validation](../validation.md).

- Один и тот же context/body в новом соединении после reopen возвращает точно исходный result и event IDs; handler вызван один раз. Повтор после постороннего события также успешен. Изменение payload, actor, role, action либо исходной expected revision даёт conflict без handler; неподдерживаемая version отклоняется при проверке envelope.
- Две connections одновременно доставляют одинаковый command ID/body: один commit, два одинаковых ответа. Два разных IDs претендуют на одну expected revision: проходит один, второй получает conflict, budget/reservation создаётся один раз. Проверять барьер до `BEGIN`, не внутри handler, чтобы тест не организовывал deadlock.
- Составная команда создаёт два события и одну receipt атомарно. Ошибка после первого события, подавленная ошибка append, несериализуемый result и ошибка INSERT receipt оставляют исходную историю. Повтор того же ещё не принятого запроса допускается после устранения причины.
- Stale запрос и неверная роль не вызывают handler/`Store.put`. Ошибка после успешного put оставляет допустимый orphan и ноль events/receipts. Процесс завершается после commit до получения ответа; redelivery восстанавливает result без нового события.
- Порча result, fingerprint, checksum либо event-range binding квитанции блокирует replay. Event chain corruption продолжает блокировать запись и чтение. UPDATE/DELETE квитанций запрещены обычным SQL.
- Legacy v1 fixture открывается writable и read-only; миграция сохраняет побайтовый `events.jsonl` и исходные hashes. Read-only opening не создаёт новую схему или blobs. Повтор migration не меняет существующие receipt/events. Чужая таблица с теми же именами колонок, но без уникального command ID не принимается за поддерживаемую схему.
- Kernel terminal result, `Search.select_next` и `PaperBuilder.build` проходят через общую границу; duplicate selection не резервирует бюджет повторно, duplicate paper не создаёт новые artifacts. Новое evidence после paper receipt оставляет старый результат историческим и блокирует актуальное materialization.

После реализации семантического изменения обязательны полный `python -m unittest discover -s tests -v` и CLI integration проверки новых storage/interface contracts. Эти тесты не подтверждают научную независимость агентов, качество гипотез или readiness manuscript для TMLR/ICLR.
