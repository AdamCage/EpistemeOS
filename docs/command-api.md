# Локальные команды v1

Статус обновлён 3 октября 2026. `CommandService` и CLI `command` добавляют идемпотентную доставку к локальному Planning/Kernel/Search/Execution/Batch/DomainBinding/Replanning/Followup/ReviewAssignment/PaperBuilder. Это запись перехода состояния; исполнение процесса, LLM-вызов и публикация не входят в handler. [ADR 0001](decisions/0001-command-admission.md) описывает транзакционную границу, [transport schema](../schemas/command-v1.schema.json) — оболочку запроса.

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

`context.actor` и поля payload `reviewer_actor`, `executor`, `replicator` новой команды должны быть каноническими: 1–128 символов ASCII в нижнем регистре, цифры и `._@:-`, первый и последний символ — буква или цифра ([ADR 0018](decisions/0018-claim-families-and-review-admission.md)). Проверка стоит после fast path replay, поэтому исторический envelope с прежним ID возвращает свою receipt. Независимость reviewer от авторов evidence на новых записях сравнивается по ключу `NFKC(id).casefold().strip()`. Это не аутентификация: ID по-прежнему заявляет caller. `study_id` и `correlation_id` — обязательные непустые metadata. `causation_id` — `null` либо ID предшествующего immutable event; это ссылка на происхождение команды, не доказательство научной причинности. Эти поля не создают изоляцию studies или аутентификацию actors. Event envelope v1 не переписывается: metadata находятся в связанной квитанции.

## Действия

Имена полей `request.payload` совпадают с аргументами указанного метода без `self`. CommandService проверяет известные имена, обязательность, JSON-типы и роль до входа в обработчик. Domain validation повторяется внутри SQL-транзакции.

| Action | Допустимая роль | Результат |
|---|---|---|
| `batch.plan` | planner | Frozen roster primary/reanalysis, назначенные actors, recipe и резерв `enqueued_attempt` |
| `domain.bind` | planner | До run/batch сохраняет frozen ручной domain recipe и точные batch параметры; не научный verdict |
| `proposal.prepare_next` | planner | Выбор winning applied experiment proposal и полный frozen batch одной receipt; без worker dispatch |
| `batch.enqueue_slot` | Назначенный executor / replicator | Atomic run + job + уникальная slot binding |
| `batch.settle` | Автор batch, planner | Atomic полный batch settlement + search terminal; claim/review не создаются |
| `analysis.apply` | analyst | Frozen proposal + полный bounded claim одной receipt после mechanical gate; не verdict |
| `execution.enqueue` | executor / replicator | Atomic новый run + frozen job, занятый protocol attempt slot |
| `execution.dispatch` | Назначенный executor / replicator | Durable намерение однократного запуска; handler не запускает процесс |
| `execution.finalize` | Назначенный executor / replicator | Atomic проверенные result + execution_finalized |
| `planning.question`, `planning.explanation_set` | planner | Immutable версия вопроса/набора с prior refs и revision reason |
| `kernel.preregister_for_set` | planner | Protocol, привязанный к текущему question/set; hypotheses и scope выводятся из них |
| `kernel.hypothesis`, `kernel.preregister` | planner | Event ID |
| `kernel.start_run` | executor для primary; replicator при `replicate_of` | Run ID; процесс ещё не запущен |
| `kernel.finish_run` | Назначенный executor/replicator | Result ID |
| `kernel.claim` | analyst, executor | Proposed claim ID |
| `kernel.review` | reviewer | ID отрицательного review (`request_changes`/`reject`) на immutable basis; `approve` отвергается (ADR 0018) |
| `kernel.link_claims` | planner, analyst | Immutable proposal связи двух claims на ожидаемых bases |
| `kernel.review_with_links` | reviewer | Отрицательный review v2 с явной оценкой каждой связи в evidence context; `approve` отвергается |
| `review.assign` | planner | Frozen manifest предполагаемого начального reviewer context и assignment event одной receipt; без права доступа или verdict |
| `review.dispatch`, `review.finalize` | planner | Сохранённая выдача projected context и raw response/status; неизвестный исход не повторяется автоматически |
| `review.submit` | reviewer | Verdict из завершённой выдачи на текущем basis, typed obligations при отрицательном ответе и связанная provenance receipt; единственный путь записи approval |
| `replanning.record_review` | reviewer | Negative review и typed открытые `review_obligation` в одной receipt |
| `replanning.resolve_obligation` | reviewer | Закрыт для новых записей (ADR 0018): resolution пишет `review.submit` под `veto_reconsideration_v1`; replay исторических receipts возвращает их, а resolutions v1 имеют статус `not_admissible` |
| `followup.apply` | planner | Frozen дочерний protocol/node и binding к одному obligation; не закрывает его |
| `followup.prepare_next` | planner | Текущий winning follow-up node и полный frozen batch в одной receipt; без worker dispatch или закрытия obligation |
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

## Назначение reviewer и граница контекста

`analysis.apply` принимает `batch`, exact `expected_settlement`, proposal schema v1, CAS `adapter_source_digest` и предполагаемый `reviewer_actor`. Оно доступно analyst только после полного технического settlement batch, созданного из frozen applied или вручную bound domain recipe. `domain.bind` принимает `protocol`, `adapter_id`, `adapter_version`, `adapter_source_digest`, `recipe` (bounded JSON), `recipe_artifacts` (CAS digests) и точный batch recipe: `reanalysis_implementation`, `reanalysis_environment`, `outputs`, `wall_seconds`, `max_output_bytes`, `required_capabilities`. Протокол должен быть planning-bound в той же study, а привязка должна предшествовать run/batch plan. Универсальный kernel не интерпретирует доменные поля `recipe`; это обязанность адаптера. Атомарная `analysis.apply` receipt связывает `[claim, batch_analysis]`; proposal/code сохранены в CAS, каждый завершённый run входит в claim evidence, mechanical gate и отсутствие reviewer среди contributors перепроверяются перед commit. Результат содержит claim, analysis event, proposal digest, task ID и evidence `basis_hash`; `scientific_validity` остаётся `not_assessed`. `episteme analysis advance BATCH --root ROOT --planner PLANNER_ID --analyst ID --reviewer ID` после перезапуска продолжает этот переход и отдельную `review.assign`; переданный planner ID должен совпасть с автором batch plan. С шага 5 [ADR 0016](decisions/0016-domain-pack-contract.md) adapter или пакет берётся из привязки protocol (`domain.bind`, модельное применение или `pack_binding`); `--adapter` — необязательное утверждение, и отличное от привязки значение отвергается без новых событий. ID, версия и bytes исходника legacy-адаптера должны совпасть с frozen `domain.bind`, иначе команда отклоняется без новых событий. `analysis status` читает сохранённое состояние. [ADR 0013](decisions/0013-batch-analysis-admission.md), [ADR 0014](decisions/0014-manual-domain-binding.md) и [ADR 0015](decisions/0015-afterlife-historical-pilot.md) описывают replay и границу доверия.

### DomainPack: `pack.preregister` и `pack.analyse`

Контракт — [ADR 0016](decisions/0016-domain-pack-contract.md). `pack.preregister` (роль `planner`) принимает ровно `explanation_set`, `pack_id`, `pack_version`, `pack_code_digest`, `parameters`, `host_inputs`, `capture` (CAS digest `CaptureBundle` либо null) и `environment` (digest frozen local Python environment). Пакет берётся только из явного реестра. Его код на диске должен совпасть с переданными идентичностью и digest; ядро исполняет именно хешированные bytes. `parameters` проверяются schema каталога и hook пакета. Compile hooks дают `ProtocolDraft` и `ExecutionPlan`; программы, input, код пакета и envelopes записывает в CAS ядро. Одна receipt связывает `[protocol, pack_binding]`: protocol создаётся через `preregister_for_set`, привязка фиксирует pin, digests каталога, host inputs, draft и plan, `roster_semantics`, `sample_size_scope`, профиль исполнения, `pack_trust=trusted_local_code` и `hook_isolation=in_process`. Результат: `{protocol, binding, pack_code_digest}`. Затем обычные `search.add_node`/`select_next` и `batch.plan`; для pack-bound protocol поля batch обязаны равняться закреплённому plan, а живой код пакета — pin. На таком protocol `domain.bind`, `kernel.claim` и ручной `kernel.start_run` отвергаются.

`pack.analyse` (роль `analyst`) принимает ровно `batch`, `expected_settlement`, pin пакета (`pack_id`, `pack_version`, `pack_code_digest`), `checks`, `recomputations`, `report` (frozen `AnalysisReport` v2), `statistical_report` (`StatisticalReport` v1) и `reviewer_actor`. Внутри команды ядро сверяет pin с привязкой и с кодом на диске, проверяет envelopes и вычисляемые им поля (planned units, отсутствующие slots, hash stopping rule, записанные метрики) и потолок силы claim. Затем оно повторно исполняет analysis hooks на том же snapshot и допускает только побайтно равные envelopes. Предложение выше потолка отвергается целиком. Receipt связывает `[claim, pack_analysis]`; результат — `{analysis, claim, basis_hash, report, task_id, scientific_validity}`. Python-controller `analysis_controller.advance_pack_analysis` исполняет hooks вне транзакции, допускает анализ, затем отдельной `review.assign` назначает reviewer и после перезапуска продолжает с сохранённой receipt. Replay обеих команд структурный и код пакета не импортирует. В CLI этот путь вызывает `episteme analysis advance`, если protocol batch имеет `pack_binding`. Read-only `episteme pack describe PACK_ID` печатает manifest, каталог, `code_manifest` и `pack_code_digest` живого зарегистрированного пакета. Read-only `episteme pack verify --root ROOT` повторно исполняет закреплённые hooks каждой привязки и каждого анализа и сравнивает bytes с записанными; код возврата 1 означает расхождение или изменённый код пакета. `episteme pack capture PACK_ID --source DIR --root ROOT` для пакета с `capture=true` один раз читает источник и записывает bytes и frozen `CaptureBundle` в CAS без событий; полученный digest передаётся в поле `capture` команды `pack.preregister`.

`review.assign` принимает ровно `claim`, `reviewer_actor`, `expected_basis`. Роль команды — `planner`; её `context.study_id` должен совпадать с bound planning study, если такой binding есть. На момент записи claim обязан пройти mechanical gate с этим basis, а reviewer ID не должен входить в авторов его evidence context. Результат `{ "assignment": "review_assignment-...", "bundle": "<sha256>" }` ссылается на одно событие и CAS JSON. Replay исходного command ID возвращает ту же историческую receipt, а не утверждение, что basis всё ещё актуален.

С шага 8 [ADR 0018](decisions/0018-claim-families-and-review-admission.md) новые назначения строят manifest `blind_initial_review_v2` (или `veto_reconsideration_v1` с полем `projection=blind_initial_review_v2`): к полям v1 он добавляет `family` (protocols семейства, реестр всех попыток — с failed, `unknown`, `queued` и незавершёнными ручными runs, записанными метриками и причинами, без implementation, environment, commands и logs — и `family_ledger_digest`), `related_registrations` (protocols с общими hypotheses, без artifacts), `linked_open_findings` (открытые отрицательные мнения о связанных claims вне семейства), `pack_reports` (StatisticalReport без `details` и потолок ядра) и `analysis_provenance`. Allowlist охватывает `raw_data` и `metrics` всех runs семейства. Approval из такого назначения обязано по ID подтвердить только `linked_open_findings` (review получает `review_schema_version=3`). Approval перестаёт засчитываться, если в семействе появился терминальный result, которого не было в назначенном контексте; `review.submit` такое approval отвергает. Исторические назначения воспроизводятся по записанной `projection`. С шага 10 `analysis status` и controllers анализа не считают выполненным назначение без submission, approval из которого не может быть засчитан (контекст v1 без runs семейства или устаревший реестр). `analysis status` показывает такие назначения в `superseded_assignments` с причиной и возвращает `awaiting_assignment`, а `analysis advance` назначает того же reviewer заново, уже с manifest v2. Read-only `episteme analysis verify --root ROOT` повторяет пересчёт каждого `batch_analysis` зарегистрированным адаптером (как `pack verify` для пакетов) и возвращает `matched` или `mismatched` с причиной по каждому анализу; код выхода 1 при `mismatched`. Ниже описан v1. Manifest policy `blind_initial_review_v1` перечисляет разрешённые `raw_data`/`metrics` digests наблюдённых results и явные exclusions. Он содержит только выбранные поля planning, claim, protocol, run и result records; прямые implementation/environment/log/unobserved-data digests и прежние review verdicts исключены. Исторический replay сверяет manifest на исходном префиксе, exact event и receipt. Поля `identity_assurance=caller_declared` и `read_isolation=not_enforced` означают, что это **спецификация контекста**, а не защита файлов или проверка личности: reviewer, имеющий обычный доступ к тому же Store, может прочитать больше. `kernel.review`, `kernel.review_with_links` и `replanning.record_review` не требуют assignment и поэтому записывают только отрицательные мнения; approval засчитывается лишь из цепочки `review.assign` → выдача → `review.submit` ([ADR 0018](decisions/0018-claim-families-and-review-admission.md), §3.1). Подробности — [ADR 0011](decisions/0011-review-assignment-context.md).

`review.dispatch` принимает `assignment`, `provider_id`; planner receipt фиксирует CAS-запрос до внешнего вызова. `review.finalize` принимает `assignment`, `response` (CAS digest либо null при failed), `status=completed|failed` и объект `usage`; новая receipt связывает raw bytes с dispatch. Обычный Python `ReviewerController.advance` вызывает `ReviewProvider.invoke(request)` один раз вне SQL transaction; после неизвестного исхода возвращает `unknown` без повторного вызова. `reconcile` отдельно фиксирует установленный оператором исход. Запрос ограничен frozen manifest и allowlisted artifacts, но provider под той же OS identity не изолирован от остальных файлов.

`review.submit` принимает `assignment`, exact completed `response` digest и `expected_basis` от reviewer actor. Raw JSON должен соответствовать [review-response-v1](../schemas/review-response-v1.schema.json): совпадающие assignment/bundle/claim/basis/reviewer, verdict, rationale, findings и `link_assessments`. Команда повторно проверяет текущий mechanical gate, study и contributors; положительный ответ не допускает findings, отрицательный создаёт typed открытые obligations. Receipt связывает `[review, review_obligation..., review_submission]`. Повтор command ID возвращает исходный результат, а новый verdict для того же assignment запрещён. Унаследованные review commands по-прежнему работают без assignment; они не получают задним числом этот provenance. Эта привязка не удостоверяет личность или независимость model/process; подробности — [ADR 0012](decisions/0012-review-delivery-and-submission.md).

Пересмотр veto ([ADR 0018](decisions/0018-claim-families-and-review-admission.md), §3.5–3.6). `review.assign` сам выбирает policy `veto_reconsideration_v1`, если у назначаемого reviewer есть открытые отрицательные мнения или неразрешённые для этого claim obligations в семействе claim. Тогда manifest получает раздел `own_findings`, а ответ должен соответствовать [review-response-v2](../schemas/review-response-v2.schema.json). Approval в нём перечисляет в `withdrawals` ровно открытые мнения reviewer в семействе и может разрешить его obligations через `resolutions` видов `discriminating_experiment` или `narrow_claim`. Отрицательный verdict передаёт пустые списки; вне reconsideration допустим только ответ v1. Withdrawal снимает veto только для этого claim. Все новые submissions имеют schema 2 с полями `policy`, `withdrawals`, `resolutions`, `family_ledger_digest` и `analysis_verification`; результат команды дополнительно содержит `resolutions`. Повторное назначение того же reviewer на тот же claim и basis разрешено, пока ни одно из них не получило submission, а завершённый, но не отправленный отрицательный ответ считается veto.

`gate.open_context_reviews` содержит открытые отрицательные reviews связанных claims. Для `approve` каждый такой ID должен быть явно указан в `evidence` хотя бы одной оценки связи; rationale объясняет совместимость с текущим ограниченным выводом. Это acknowledgement не закрывает исходное veto связанного claim. Его отрицательный verdict сохраняется между evidence revisions и меняется только новым approval того же reviewer. Эпизоды отрицательного review и первое закрывающее решение входят в зависимый basis; повторные обычные approvals не создают бесконечной взаимной инвалидации. Проверка ссылок не доказывает достаточность научного ответа; адресный переход для одного вида typed finding описан ниже и в [ADR 0010](decisions/0010-evidence-bound-obligation-resolution.md).

## Совместимость и восстановление

Writable opening аддитивно создаёт таблицу квитанций и triggers; прежние v1 events, IDs и hashes сохраняются. Read-only opening старой базы не мигрирует её. Полные квитанции можно прочитать через `receipts`/`Store.receipts()` и экспортировать через `Store.export_receipts()`; они содержат нормализованный request, результат и точные связи с event range.

Обычный `events.jsonl` и review bundle **не восстанавливают историю доставки**. Для возобновления без утраты idempotency реализованы CLI `backup`/`restore` и [directory snapshot v1](recovery.md): SQLite online backup вместе с проверенными CAS artifacts, без пересоздания events или receipts. Нельзя копировать только активный `state.sqlite3`, игнорируя WAL. Восстановление требует нового каталога и поддерживаемой схемы; общий migration framework ещё не реализован. Отдельные JSONL exports events/receipts следует делать при остановленном writer, если требуется один общий snapshot.

Изменение signature, default или типа action требует новой версии command API с сохранением обработки прежних v1 запросов. Добавлять поля в старый нормализатор незаметно для caller нельзя: это изменит fingerprint при replay. Текущая версия реализует только v1.

Транзакция обеспечивает однократный commit событий при повторной доставке. Внешний запуск, API charge и файловое materialization требуют отдельного outbox/lease протокола M2. При rollback могут остаться неиспользуемые CAS blobs; частичные events и receipt не фиксируются. Actor IDs остаются заявлениями доверенного caller, а hashes/triggers не защищают от владельца всей базы.


## Model proposal commands v1

Все actions требуют planner role; после admission применяется сохранённый assignee. Это caller metadata, не аутентификация.

| Action | Payload |
|---|---|
| `agent.register_budget` | `study_id`, `max_calls` (1–100 admission в одном budget). |
| `agent.request_hypotheses` | `budget`, `question`, `assignee`, `provider` CAS digest; `wall_seconds=120` (1–600), `max_output_bytes=1048576` (1 KiB–4 MiB). |
| `agent.request_experiment` | `budget`, `explanation_set`, `tree`, `recipe_binding`, `assignee`, `provider` CAS digest; те же optional лимиты. |
| `agent.dispatch` | `request`, `workspace_token` (32 hex); один dispatch, original authority marker, current question. |
| `agent.finalize` | `request`, `manifest` CAS digest; original completion и captured bytes проверяются. |
| `agent.apply_hypotheses` | `request`; proposed response, current question, atomic hypotheses/set/application. |
| `agent.apply_experiment` | `request`; proposed response, current ExplanationSet/tree/recipe, atomic protocol/node/application. |

`episteme agent provider --root <root> --model <model>` сохраняет binary/version/profile descriptor без model call; optional `--reasoning-effort` и `--executable`. Для budget/request используется обычный command envelope. Study metadata сверяется через request/question/budget, включая существующие связи. Replay исходного envelope возвращает исторический результат и не запускает провайдера.

CLI `agent status|work|reconcile|advance <request> --root <root>` использует сохранённое назначение. `work` вызывает модель только после нового dispatch; `reconcile` никогда не запускает её; `advance` дополнительно применяет proposed response. Invalid/abstained/failed не создают hypotheses; unknown не разрешает повтор. Status `applied` не является scientific success. Contracts и границы — [ADR 0007](decisions/0007-model-proposals.md).

## Proposal selection и review-driven follow-up

`proposal.prepare_next` принимает `tree`, `executor`, `replicator`; optional `wall_seconds=120`, `max_output_bytes=1048576`, `required_capabilities=null`. Текущий best-first winner должен быть узлом ровно одного применённого experiment proposal schema v2. Команда атомарно фиксирует `[search_selection, batch_plan]`, используя замороженные reanalysis source/environment/outputs из исходного application. Если дерево даёт wait/stop или выигрывает другой узел, запись откатывается. Возвращаемый batch ID — подготовка полного roster, а не разрешение считать эксперимент выполненным. Исполнение отдельно начинает `episteme batch advance <batch-id> --root <root>`.

`replanning.record_review` принимает `claim`, отрицательный `verdict` (`request_changes`/`reject`), `rationale`, `expected_basis`, список `findings` и optional `link_assessments=null`. Каждый finding содержит ровно `kind`, `action`, `closure_criterion` и `evidence_refs` (уникальные IDs из текущего review context). Доступные виды: `discriminating_experiment`, `independent_reanalysis`, `narrow_claim`, `request_data`, `stop_inconclusive`. На текущем mechanically qualified basis команда сохраняет `[review, review_obligation...]` и возвращает `{review, obligations}`. Если context содержит связи claims, нужны полные `link_assessments` по контракту review v2. Reviewer ID проверяется на конфликт с contributors, но не аутентифицируется.

`followup.apply` принимает `obligation`, `parent_node`, текущий `explanation_set`, `protocol_spec`, `node_spec`, `expected_basis`. Первая policy поддерживает только `discriminating_experiment`; specs должен подготовить planner. Команда ещё раз проверяет исходный claim/review/basis, завершённый parent, study/scope, вместимость и бюджет дерева, затем фиксирует `[protocol, experiment_node, replan_followup]` одной receipt. `protocol_spec` содержит поля обычной preregistration без hypotheses/scope/parent — они выводятся из ExplanationSet и source protocol; `node_spec` содержит `action`, `components`, `estimated_cost`, `rationale`. Результат — ID `replan_followup`. Повтор того же obligation не создаёт второй план. Обязательство остаётся открытым, `scientific_validity=not_assessed`; текущий `next_action` и paper gate не пропускают его как закрытое. Подробные ограничения — [ADR 0009](decisions/0009-proposal-review-replanning.md).

`followup.prepare_next` принимает `obligation`, `executor`, `replicator`, `reanalysis_implementation`, `reanalysis_environment`, `outputs` и optional `wall_seconds=120`, `max_output_bytes=1048576`, `required_capabilities=null`. Planner подаёт отдельный frozen recipe: `followup.apply` сохранил protocol/node, но не runnable reanalysis source или имена файлов outputs. Команда повторно проверяет исходный review/basis и актуальный planning binding, затем принимает только тот follow-up node, который **сейчас** выигрывает best-first выбор. Batch gate проверяет обе реализации, environment, output contract и полный резерв `2 × seeds`. `[search_selection, batch_plan]` фиксируются одной receipt; при wait/stop, другом winner, stale source или неверном recipe оба события откатываются. Перед новым `batch.enqueue_slot` и `execution.dispatch` source review/basis и planning снова проверяются; поздняя ревизия запрещает новую запись job/dispatch, сохраняя исходную receipt и уже dispatched attempts для reconciliation. ID batch допускает отдельный `episteme batch advance`, но не означает исполнение, независимую репликацию или разрешение reviewer. Исторический replay сверяет исходный request, точный event prefix, решение search policy и полный receipt, не переоценивая поздние reviews задним числом.

**С шага 7 [ADR 0018](decisions/0018-claim-families-and-review-admission.md) новые записи этой командой отвергаются**: resolution записывает исходный reviewer в `review.submit` под назначением `veto_reconsideration_v1` (resolution schema 2, привязанная к паре obligation и claim). Ниже описан исторический контракт v1; его receipts воспроизводятся, а effective status таких resolutions — `not_admissible`. Исторически `replanning.resolve_obligation` принимал `obligation`, новый дочерний `claim`, его `expected_basis`, `review_rationale`, `resolution_rationale`, `evidence_refs` и optional `link_assessments=null`. Пока поддержан только bound `discriminating_experiment`. Команду может записать лишь исходный reviewer отрицательного finding; ID остаётся заявлением доверенного локального caller. Исходный source basis должен быть неизменённым, дочерний узел технически завершён, а новый claim должен пройти current mechanical gate. Для batch claim создаётся **после** полного settlement и ссылается на точный roster runs; у batch terminal поле `claim=null`. `evidence_refs` охватывают новый claim и все result events его runs, а решение сохраняет hashes цитируемых revisions. Receipt ровно `[review, review_obligation_resolution]`; второй event сообщает `reviewer_satisfied`, не объективную научную истинность. Новые evidence, claim links или поздний review могут сделать effective status `stale_resolution`, и paper gate вновь заблокирует claim. Другие открытые findings исходного claim продолжают блокировать descendant. Подробнее — [ADR 0010](decisions/0010-evidence-bound-obligation-resolution.md).

`episteme followup status <obligation-id> --root <root>` читает связанный план без записи и показывает текущий `obligation_resolution`: `open`, `reviewer_satisfied` или `stale_resolution`, а также ID исторического resolution при его наличии. Все effective открытые obligations исходного claim наследуются descendant claim на протоколе follow-up, даже если у исходного claim несколько findings и successor получил собственный approval; paper gate их не закрывает.

Тот же запрет распространяется на claim, чей protocol является созданным follow-up или его потомком, даже если явной связи `supersedes` ещё нет.
