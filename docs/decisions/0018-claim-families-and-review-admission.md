# ADR 0018 — семейства claims и допуск reviews к решениям

Дата: 4 октября 2026. Статус: **Принято 4 октября 2026 года координирующим агентом по делегированию пользователя; реализуется поэтапно.** Пользователь делегировал проектные решения координатору. Координатор принял документ в изложенном виде, включая все спорные решения раздела 13, и добавил одно ограничение (раздел 12, недоступность исходного reviewer). Что реализовано и проверено, а что только запланировано, указано в разделе «Ход реализации»; результаты проверок — в [validation.md](../validation.md). Основания: [состязательный аудит `da6aa2a`](../adversarial-audit-2026-10-04.md) (находки A-01–A-10, A-14, A-22), [архитектура](../architecture.md) (§3, §6, §7, §10), [MVP-план](../mvp-plan.md) (M1, M4, M5), ADR [0002](0002-statistical-design.md), [0003](0003-claim-relations.md), [0009](0009-proposal-review-replanning.md), [0010](0010-evidence-bound-obligation-resolution.md), [0011](0011-review-assignment-context.md), [0012](0012-review-delivery-and-submission.md), [0013](0013-batch-analysis-admission.md), [0016](0016-domain-pack-contract.md). Предложение написано по коду `da6aa2a`, и номера строк относятся к этому commit. Параллельная работа [ADR 0017](0017-incremental-verification.md) меняет `store.py`, `graph.py` и `cli.py`; зависимости перечислены в разделе «Связь с ADR 0017».

## Проблема

Аудит воспроизвёл шесть находок high и четыре medium в области review, claims, gates, obligations и paper. За ними стоят три общие причины.

1. **Решение привязано к одному ID claim и одному protocol, а evidence разделяются.**
   - Veto и открытые obligations ищутся по `payload.claim` (`Kernel._next_action`, `kernel.py:823-857`).
   - Resolution засчитывается без сверки с claim, который она оценивала (`open_obligations`, `replanning.py:140-148`; `_paper_followup_lineage`, `reporting.py:258-329`).
   - Потолок пакета проверяется только для protocol с собственной `pack_binding` (`kernel.py:455-458`, `batch.py:298-299`).
   - Слепой bundle показывает runs только протоколов claims связанного контекста (`review_assignment.py:70-83`), а paper — только протокола claim (`reporting.py:478`).

   Поэтому повторный claim на тех же runs обходит veto (A-01), resolution узкого claim открывает соседний (A-02), amendment снимает потолок (A-03), а противоречащая попытка под другим protocol на тех же bytes не видна ни reviewer, ни читателю paper (A-05).
2. **Read side принимает наличие события `review`, а не его происхождение.** `next_action`, `PaperBuilder` и Graph засчитывают:
   - approval из legacy `kernel.review` и CLI `review` без assignment;
   - approval, записанное другим actor под чужим ID (A-06);
   - событие, которое автор primary run дописал через `Store.append` с ролью executor (A-10).

   Строки actor не нормализуются, поэтому пробел или гомоглиф делает contributor «независимым» reviewer (A-22).
3. **Происхождение интерпретации и чисел не перепроверяется в момент сохраняемого перехода.**
   - `analysis.apply` записывает предложение, которого адаптер не вычислял, под ID и digest адаптера (A-04).
   - Paper рисует failed-попытку прочерками, хотя её метрика записана, и не показывает повтор seed (A-07).
   - Копия digests оригинала проходит как «независимый реанализ» (A-08).
   - Копия просмотренных данных, перекодированная на один байт, проходит как свежий confirmatory holdout (A-09).

Отдельно аудит показал две проблемы слепого review (A-14): слепой bundle несовместим с обязательным acknowledgement чужих замечаний, а повторное назначение после неудачной отправки запрещено.

При проектировании найден ещё один путь A-03, которого в аудите нет. `followup.apply` создаёт дочерний protocol через `preregister_for_set(parent=...)` и не проверяет привязку пакета. Скрипт на снимке `da6aa2a` во временном каталоге показал следующее. Для claim, допущенного `pack.analyse`, отрицательный typed review и `followup.apply` создают дочерний protocol без `pack_binding`. `validate_start` разрешает на нём ручные runs, а `kernel.claim` записывает `supports` по сфабрикованным outputs, и mechanical gate проходит. До paper такой claim в `da6aa2a` удерживают открытые obligations исходного claim, но потолок пакета уже обойдён.

Этот ADR меняет семантику решений. Он не добавляет аутентификацию, изоляцию процессов или защиту хранилища: actor IDs остаются заявлениями доверенного caller. Находки уровня хранилища и исполнения перечислены в разделе «Связанные находки».

## Решение

### 1. Семейство claim

Новый модуль `review_admission.py` вычисляет семейство чистой функцией `claim_family(history, claim)` по событиям снимка, без чтения CAS. Её одинаково используют `Kernel._next_action`, `PaperBuilder`, `review.assign`, `review.submit`, export и `analysis status`.

Для protocol `P` обозначим `bytes(P)` объединение трёх множеств: `P.data`, digests всех `data_splits` typed design и `raw_data` всех results runs `P` при любом статусе. Два protocol соединены ребром, если выполнено хотя бы одно условие:

| Ребро | Условие | Что закрывает |
| --- | --- | --- |
| amendment | `Q.parent == P`; сюда входят дочерние protocols `followup.apply`, которые записывают `parent` | A-03; повторный claim на amendment |
| follow-up | `replan_followup.protocol == Q`, а claim его obligation записан на `P` | follow-up без `parent` на будущих путях |
| общие bytes | `bytes(P) ∩ bytes(Q) ≠ ∅` | A-05; перерегистрация на тех же данных |
| supersession | `claim_link` с relation `supersedes` между claim на `P` и claim на `Q` | явная замена утверждения |

Линия protocols `K(P)` — компонента связности этого неориентированного графа. Семейство `F(c)` — все claims на protocols из `K(protocol(c))`, в порядке событий и включая сам `c`.

Семейство не создают:

- связи `supports`, `limits` и `contradicts`. Это разные утверждения, и они остаются review context ADR 0003. Иначе veto одного claim блокировало бы claim, который с ним спорит;
- декларации `seen_data` и события `data_exposure`. Это журнал экспозиции. `seen_data` нового protocol наследует все известные просмотры локального Store и склеил бы несвязанные исследования в одно семейство;
- digests implementation и environment;
- общая planning lineage. Она даёт только «связанные регистрации» (см. ниже).

Семейство монотонно: append-only история только добавляет рёбра, и замечание не выпадает из семейства со временем. Стоимость вычисления — O(история).

Та же функция возвращает **связанные регистрации** `R(c)`: protocols вне `K`, которые либо planning-bound с тем же `study_id` и тем же корневым research question, либо legacy с тем же множеством hypothesis IDs. Они нужны только для раскрытия (§4). Reviewer и читатель paper видят попытки на других bytes по тому же вопросу, но такие попытки не блокируют решения и не делают их устаревшими.

### 2. Veto, obligations и resolutions по семейству (A-01, A-02)

Определения для снимка истории и claim `c`:

- **Отрицательное мнение** — событие `review` с ролью `reviewer` и verdict `request_changes` или `reject`, записанное любым путём. К ним же относится завершённая выдача `review_response`, если её ответ разбирается как review response с отрицательным verdict, а через `review.submit` он не отправлен (§3.6). Assignment и receipt для отрицательного мнения не требуются, чтобы отсутствие provenance не снимало замечание.
- **Допустимое approval** определено в §3.1. Approvals, не прошедшие предикат, ничего не открывают и ничего не снимают.
- Reviewers сравниваются по нормализованному ключу ID (§3.4).
- **Открытое отрицательное мнение** reviewer `k` о claim `x` — последнее отрицательное мнение `k` о `x`, после которого у `k` нет допустимого approval `x`.
- **Veto.** Reviewer `k` блокирует claim `c`, если у `k` есть открытое отрицательное мнение `n` о каком-либо `x ∈ F(c)` и `k` не отозвал `n` для `c`. Отзыв записывается только допустимым approval `c` под policy `veto_reconsideration_v1` (§3.5). Для `x = c` отзывом служит допустимое approval самого `c`.
- **Блокирующее obligation.** Obligation `o`, проверенное replay, как сейчас (`replanning._index`), блокирует `c`, если `o.claim ∈ F(c)` и для пары `(o, c)` нет действующей resolution. Действующая resolution — событие `review_obligation_resolution` с `obligation = o` и `claim = c`. Её approval должно быть допустимым, а effective status, пересчитанный по правилам ADR 0010, — `reviewer_satisfied`.

Resolution действует только для claim, который оценил resolving reviewer. Одно obligation получает отдельную resolution для каждого claim семейства. Resolution для одного claim не переносится ни на соседний claim, ни на потомка, ни на исходный claim.

**Отклонение (шаг 2).** До этого ADR открытые obligations claims связанного контекста (`resolve_context`: `supports`, `limits`, `contradicts`, `supersedes`) тоже блокировали claim. Определение выше перечисляет только семейство и молча отменило бы это правило; это ослабление, которого ADR не обсуждает. Реализация его сохраняет: obligation связанного claim вне семейства блокирует, пока у obligation нет действующей resolution (прежнее правило, без пары). Связанные claims по-прежнему не образуют семейства и не передают veto.

Порядок `next_action` прежний:

1. собственный gate не прошёл → `repair_evidence`;
2. есть блокирующие obligations семейства → `replan`;
3. есть veto → `replan`;
4. нет свежего допустимого approval `c` → `scientific_review`;
5. есть принятая supersession → `superseded`;
6. иначе → `paper_candidate`.

Для прежних ситуаций форма ответа не меняется. Новые ключи появляются только в новых ситуациях: `family_vetoes` — veto наложено через другой claim семейства; `uncounted_reviews` — у `c` есть недопустимые approvals; `disclosures` и `family_ledger` — в `paper_candidate` (§4). `PaperBuilder.build`, `materialize` и `_paper_followup_lineage` используют ту же функцию решения. Шаг lineage требует `resolution.claim == claim`, иначе отказывает.

Так разрешаются сценарии аудита и прежние пути:

- **A-01.** У R1 открытое отрицательное мнение о A, а B ∈ F(A), потому что у них общий protocol. Слепое approval R2 не снимает veto R1, поэтому B получает `replan`, и paper отвергается. Открытые obligations A тоже блокируют B.
- **A-02.** Resolution выдана для C1. Для C2 на том же дочернем protocol obligation остаётся открытым, а veto R1 не отозвано, поэтому C2 получает `replan`.
- **Перерегистрация на тех же bytes** попадает в семейство через ребро общих bytes.
- **Обычный путь ADR 0010.** R1 одобряет C1 под `veto_reconsideration_v1`. Одна receipt отзывает veto R1 на A для C1 и разрешает finding для C1. Остальные obligations A по-прежнему блокируют C1, а сам A остаётся заблокированным.

Изменения семантики прежних ADR:

- **ADR 0003.** Собственное veto действует на всё семейство и снимается только явным отзывом владельца; прежде его снимало любое позднее approval того же actor. Обязательный acknowledgement чужих открытых замечаний сохраняется только для связанных claims вне семейства (§4.3). Рецепт basis, включая `_context_findings`, не меняется.
- **ADR 0009.** Правило «все obligations исходного claim блокируют потомка» распространяется на всё семейство.
- **ADR 0010.** Resolution выдаётся на пару (obligation, claim), а не одна на obligation. Claim на protocol глубже дочернего follow-up получает собственную resolution. Сейчас его покрывает resolution ребёнка; это фиксирует тест `test_descendant_protocol_paper_keeps_ancestral_review_lineage`.
- **Guard `link_claims`** (`kernel.py:584-593`) проверяет владельцев открытых отрицательных мнений во всех затронутых семействах.

### 3. Допуск reviews при чтении (A-06, A-10, A-22)

Mechanical gate reviews не учитывает, и это не меняется. Предикат ниже определяет, какие reviews учитывают `next_action`, paper, `analysis status` и review assignment.

#### 3.1 Предикат допустимого approval

Approval claim `c` допустимо, если выполнены все условия:

1. **Привязка к выдаче.** Событие — `review` из receipt `review.submit`, прошедшей replay `review_submission._index`. Значит, оно связано с assignment, dispatch и завершённым response, имеет роль `reviewer` и basis assignment, а его actor равен `reviewer_actor` assignment.
2. **Независимость.** ID reviewer канонический (§3.4), и его нормализованный ключ не совпадает с ключами contributors review context `c` на префиксе события.
3. **Policy.** Assignment имеет policy `blind_initial_review_v2` или `veto_reconsideration_v1`. Assignment с policy `blind_initial_review_v1` допускается, только если он полон для семейства: все runs линии `K` на его префиксе перечислены в `observed_runs` manifest.
4. **Происхождение анализа.** Если claim допущен legacy `batch_analysis` v1 с непроверенным происхождением, submission обязана записать `analysis_verification=recomputed_match` (§5.1).
5. **Собственные замечания.** Если у reviewer есть открытые отрицательные мнения в `F(c)`, policy должна быть `veto_reconsideration_v1`, а отзывы должны покрывать их все. Это проверяется при submit и повторяется при replay. Для submissions schema 1 условие проверяется на их префиксе.

Для `paper_candidate` approval должно быть ещё и свежим. Текущий basis `c` равен basis approval, как сейчас, а связанная часть реестра попыток семейства (§4.1) равна реестру на префиксе submission. Для submissions schema 2 сравнивается записанный в них digest.

#### 3.2 Пути записи

| Путь | Approve | Отрицательное мнение |
| --- | --- | --- |
| `review.assign` → `review.dispatch`/`review.finalize` → `review.submit` | допустимо по §3.1 | veto и typed obligations |
| Завершённый отрицательный ответ без `review.submit` | — | veto без obligations |
| `replanning.record_review` | путь только для отрицательных мнений | veto и typed obligations |
| Команды `kernel.review`, `kernel.review_with_links` | новая запись отвергается; исторические approvals становятся advisory | veto |
| CLI `review`, прямой `Kernel.review` | новая запись отвергается без событий (CLI возвращает код 2); исторические approvals становятся advisory | veto, как сейчас |
| `replanning.resolve_obligation` (v1) | новая запись отвергается; исторические resolutions получают статус `not_admissible` | — |
| `Store.append` в обход команд | advisory | veto, если роль `reviewer` |
| Любой путь, если роль не `reviewer` или автор — contributor по точной строке | Graph, export, backup и решения отказывают | то же |

Actions остаются в списке `CommandService`, поэтому повторная доставка исторического envelope возвращает его receipt: fast path `Store.command` срабатывает раньше handler. `ReviewSubmission.submit` вызывает внутренний метод `Kernel`, которому разрешено записать approval. Публичные `review` и `review_with_links` отвергают `approve` и указывают на путь assignment. `Resolution.resolve_obligation` отвергает новые записи, а её replay не меняется.

Опубликованный контракт v1 этим сужается: `kernel.review` и `kernel.review_with_links` больше не принимают `approve`. Это сознательное решение. Если принимать и молча игнорировать новые approvals, журнал копил бы записи «approve», которые ничего не значат.

#### 3.3 Reviews, дописанные в обход команд

Ни один путь ядра не записывает `review` с ролью, отличной от `reviewer`, или от actor, чей ID по точной строке совпадает с автором evidence context. Это проверяют `_record_review`, `review_submission._decision` и `resolution._review_payload` на том же префиксе. Поэтому Graph, а за ним export и backup, отвергают такие события как структурно невозможные. Проекция допуска, на которой основаны `next_action`, paper и assignment, тоже отказывает (fail closed). Это закрывает PoC A-10.

Остальные события, дописанные в обход команд, становятся advisory approvals или отрицательными мнениями. Поддельный `review_submission` replay уже отвергает как orphan. Согласованно подделанная receipt относится к границе хранилища (A-13, A-19), и этот ADR её не закрывает.

#### 3.4 Actor IDs (A-22)

- **Канонический вид для новых записей:** `^[a-z0-9](?:[a-z0-9._@:-]{0,126}[a-z0-9])?$` — ASCII в нижнем регистре, 1–128 символов, без пробелов. Где проверяется:
  - внутри транзакции команды, после fast path replay;
  - в прямых записях `Kernel`;
  - в actor-полях payload (`reviewer_actor`, `executor`, `replicator`);
  - в аргументах controllers и флагах CLI `--actor`, `--planner`, `--analyst`, `--reviewer`.
- **Сравнение по ключу `NFKC(id).casefold().strip()`.** По нему сравниваются независимость, участие в evidence и владение veto. Ключ используют:
  - `_review_members` и guard `link_claims`;
  - `review.assign` и `review.submit`;
  - проверка reviewer в `analysis.apply` и `pack.analyse`;
  - resolution.
- **Исторические неканонические IDs.** Вклад такого actor учитывается по ключу. Его approvals недопустимы, потому что различимость identity нельзя проверить. Его отрицательные мнения сохраняют силу.
- **Граница гарантии.** Ядро обеспечивает только разделение reviewer и contributors. Совпадение analyst с planner или executor допустимо: `kernel.claim` и сейчас принимает роль executor. Paper и export показывают для семейства таблицу «actor → роли», чтобы такие совпадения были видны.

#### 3.5 Пересмотр veto и resolutions через `review.submit`

- **Выбор policy.** Поля запроса `review.assign` не меняются, а policy выбирается детерминированно на снимке:
  - `veto_reconsideration_v1`, если у назначаемого reviewer в `F(c)` есть открытые отрицательные мнения или собственные obligations, не разрешённые для `c`;
  - иначе `blind_initial_review_v2` (§4.2).

  Выбор сохраняет поле `policy` события. Replay восстанавливает manifest по этому полю, и код v1 остаётся замороженным.
- **Manifest `veto_reconsideration_v1`** — проекция `blind_initial_review_v2` плюс раздел `own_findings`. В него входят только собственные отрицательные мнения reviewer в `F(c)` (verdict, rationale, actions) и его obligations (kind, action, closure criterion, цитаты, привязанный follow-up, статус resolution для каждого claim). Мнения других reviewers в manifest не попадают.
- **Ответ v2** (`schemas/review-response-v2.schema.json`) — поля v1 плюс `withdrawals: [{opinion, rationale}]` и `resolutions: [{obligation, rationale, evidence_refs}]`. Оба списка допустимы только при verdict `approve` под reconsideration. `withdrawals` перечисляет ровно открытые отрицательные мнения reviewer в `F(c)`. Resolutions необязательны; для каждой ядро проверяет условия её вида:
  - `discriminating_experiment` — условия ADR 0010 с одним расширением: claim может лежать не только на дочернем protocol follow-up, но и на его потомке. Нужны завершённый terminal follow-up, новые results после follow-up и цитаты на claim и все его results.
  - `narrow_claim` — claim записан после obligation, входит в семейство исходного claim, имеет тот же scope, а цитаты включают claim.
  - Остальные виды этим ADR не разрешаются и остаются открытыми.
- **Receipt `review.submit`** — `[review, review_obligation_resolution…, review_submission]`:
  - `review_submission` schema 2 добавляет поля `policy`, `withdrawals` (IDs и hashes мнений, rationale), `resolutions`, `family_ledger_digest` и `analysis_verification`;
  - `review_obligation_resolution` schema 2 ссылается на assignment, а для `narrow_claim` хранит `followup` и `terminal`, равные `null`;
  - review из submission schema 2 получает `review_schema_version=3`. Graph применяет к таким reviews правило acknowledgement этого ADR, а к прежним — их исходное правило.
- **Replay.** `review_submission._index` разбирает submissions schema 1 и 2. `resolution._index` проверяет resolutions v1 по receipts `replanning.resolve_obligation`, а v2 — внутри receipts `review.submit`.

#### 3.6 Повторное назначение и неотправленные отрицательные ответы (A-14)

Сейчас повторное назначение того же reviewer на тот же claim и basis запрещено: `review_assignment.py:236-240`, а при replay — проверка уникальности в строках 212-215. Новое правило разрешает его, если ни одно прежнее назначение с тем же ключом не получило submission. Все выдачи и ответы сохраняются.

Завершённый отрицательный ответ, который не отправили, считается veto его `reviewer_actor`. Поэтому повторным назначением нельзя ни подобрать нужный verdict, ни подавить отрицательный ответ. Смена basis такое veto не снимает: как и прежде, veto от basis не зависит.

#### 3.7 Synthetic demo

`PaperBuilder.build` отвергает claims, у которых в scope `mode == "synthetic_demo"`. Контракт запрещает изготавливать approvals для demo-вывода, и система не должна собирать paper из demo даже по формально полной цепочке. Ограничение demo «no reviewer approval is fabricated» остаётся верным.

### 4. Слепота и полнота (A-05, A-07, A-14)

#### 4.1 Реестр попыток семейства

`attempt_ledger(store, history, claim)` строит упорядоченный реестр всех runs линии `K` и связанных регистраций `R`. Каждая запись содержит:

- run, protocol, seed, вид (primary или реанализ), `replicate_of` и номер primary-попытки для пары (protocol, seed);
- статус: `completed`, `failed`, `cancelled`, `unknown`, `queued` или незавершённый ручной run;
- записанные digests `raw_data` и `metrics` при любом статусе и значение primary metric, если оно конечно;
- причину failed или cancelled, до 512 символов;
- происхождение outputs: `managed`, если у run есть проверенный `execution_finalized`, иначе `caller_declared`;
- для реанализа — `replication_mode=same_data_reanalysis` и флаг `metrics_artifact_shared_with_original`;
- смысл повторов roster: для пакетов — `roster_repetition` из фактов replication ADR 0016, для legacy — `undeclared`;
- ярус (`family` или `related_registration`) и метку ребра, по которому protocol попал в линию.

Ни одна запись не получает `exact_rerun` или `new_data_replication`: текущие gates поддерживают только повторный анализ тех же данных другой программой.

Связанная часть реестра — канонический список пар (run, result) для всех runs линии `K` с терминальным result. Её digest `family_ledger_digest` записывается в manifest v2 и в submission. Новый терминальный result в семействе делает прежние approvals несвежими для paper — так же, как сейчас это делает смена basis. Незавершённые попытки и связанные регистрации раскрываются, но в digest не входят.

Рецепт basis не меняется. Для legacy protocols чужие попытки на тех же bytes по-прежнему не входят в basis. Вместо этого они входят в связанный реестр, и approval, выданное до них, перестаёт быть свежим.

#### 4.2 Policy `blind_initial_review_v2`

Manifest v2 содержит поля v1 и дополнительно:

- `family`: protocols линии с теми же safe-полями, что в v1, реестр попыток без implementation, environment, commands, logs и specifications, и `family_ledger_digest`;
- `related_registrations`: safe-поля protocols, статусы runs и значения primary metric, без artifacts;
- `linked_open_findings`: открытые отрицательные мнения о связанных claims **вне** семейства (ID, verdict, rationale, actions). Без них approval не может выполнить acknowledgement ADR 0003;
- для claims пакета — `StatisticalReport`, `ceiling` и строки потолка ядра. Это решение 4 ADR 0016: report передаётся без исходника пакета и без `details`. Policy v2 этого ADR и v2 из шага 7 ADR 0016 объединены в одну версию;
- `analysis_provenance` со значением `recomputed_by_registered_adapter_at_admission`, `recomputed_by_pinned_pack_at_admission` или `caller_submitted_unverified`.

В allowlist входят наблюдённые `raw_data` и `metrics` всех runs семейства при любом статусе. Коллизия с implementation, environment или log любого protocol и run семейства и связанных регистраций отвергается, как в v1. Прежние мнения о целевом claim и его семействе исключаются. Если в allowlist больше 256 artifacts (лимит `reviewer_controller.py`), dispatch отказывает. Большие семейства поэтому нельзя выдать reviewer до отдельного решения о порционной выдаче.

#### 4.3 Acknowledgement (A-14)

Approval с `review_schema_version=3` обязано по ID сослаться на открытые замечания связанных claims вне семейства; их IDs есть в manifest. Замечания внутри семейства другие reviewers не подтверждают: их обеспечивает veto. Поэтому слепое approval R2 допустимо и при этом не снимает veto R1.

Открытость замечания для acknowledgement определяется допустимыми approvals и отзывами. Рецепт `_context_findings` внутри basis остаётся прежним, чтобы исторические bases совпадали.

#### 4.4 Paper и export

В paper появляются разделы:

- «Семейство и реестр попыток» — все попытки, включая failed с записанной метрикой и причиной, `unknown` и `queued`, повторы seed и происхождение outputs;
- «Связанные регистрации»;
- «Допуск reviews» — засчитанные approvals с IDs assignment, dispatch и submission и с policy; незасчитанные reviews с причинами; veto и отзывы; resolutions вместе с их claim.

Ядро автоматически добавляет в limitations строки о попытках с неизвестным исходом, о повторах seed и о `caller_declared` evidence. `_run_table` показывает записанную метрику и причину для failed и cancelled. Review bundle при export и bundle paper получают `bundle_version=2` с разделами `review_admission` и `claim_families`.

Событие `paper` не меняется, а его статус выводится при чтении:

- `current` — paper eligible сейчас;
- `historical` — eligible на своём префиксе, но не сейчас;
- `not_eligible_under_current_rules` — не eligible на своём префиксе по правилам этого ADR.

`materialize` требует `current`.

#### 4.5 Повтор seed (A-07)

Для typed protocol с `stopping_rule.kind=fixed_sample` gate отказывает, если у seed больше одной primary-попытки, а более ранняя терминальная попытка записала `raw_data` или `metrics`: «seed retried after an observed outcome». Остальные случаи только раскрываются в реестре, `disclosures` и paper: технический повтор без записанных outputs, sequential design и legacy protocol, у которого stopping rule — свободный текст.

#### 4.6 Незавершённые попытки

Попытки `unknown` и `queued` в семействе раскрываются, но не блокируют paper. ADR 0005 запрещает переименовывать `unknown`, а перехода «исход утрачен» нет, так что блокировка закрыла бы семейство навсегда. Когда попытка завершается, она входит в связанный реестр. Явная запись утраченной попытки относится к ADR профиля исполнения.

### 5. Происхождение анализа и потолок пакета (A-03, A-04)

#### 5.1 `analysis.apply` пересчитывает предложение

- **Реестр адаптеров.** Явный allowlist legacy-адаптеров переносится из `cli.py:176-181` в `domains/registry.py`: `LEGACY_ANALYSIS_ADAPTERS` отображает `adapter_id` на модуль и класс, без discovery. Так ядро не называет доменные модули, как требует ADR 0016.
- **Проверка внутри команды на том же снимке.** Должно выполняться всё перечисленное, иначе команда отказывает без событий:
  - `(adapter_id, adapter_version)` совпадают с живым адаптером;
  - SHA-256 bytes модуля адаптера равен `adapter_source_digest`, а для ручной привязки — ещё и pin `domain.bind`;
  - `canonical(proposal) == canonical(adapter.propose(store, state))`.

  Образец тот же, что у `run_hooks` в `pack.analyse` (ADR 0016, уточнение шага 4).
- **Событие.** `batch_analysis` schema 2 добавляет `proposal_origin="recomputed_by_registered_adapter_at_admission"`. Replay событий v1 не меняется; такие события получают производную метку `caller_submitted_unverified`.
- **Исторические анализы v1.** Approval claim с таким анализом допустимо, только если `review.submit` пересчитал предложение внутри транзакции и записал `analysis_verification=recomputed_match`. Отрицательные verdicts пересчёта не требуют.
- **Проверка без записи.** Read-only `episteme analysis verify --root ROOT` повторяет пересчёт для всех анализов, как `pack verify`.

Пересчёт подтверждает, что claim — вывод зарегистрированного кода на этом снимке; правильность вывода он не устанавливает. Адаптер остаётся доверенным кодом в процессе ядра (A-11, A-12).

#### 5.2 Привязка пакета наследуется по линии `parent`

`pack_lineage(history, protocol)` возвращает ближайшего предка (или сам protocol) с `pack_binding`, проверенной replay. Protocol, у которого есть такой предок, называется pack-governed. При записи действуют три правила.

1. **Amendment.** `Kernel._preregister` отвергает `parent` из pack-governed линии. Одна проверка закрывает `kernel.preregister`, `kernel.preregister_for_set` и `followup.apply`. Пути amendment для пакетов пока нет. Будущий `pack.preregister` с `parent` должен заново исполнить compile hooks и записать линию.
2. **Legacy-команды на потомках.** На pack-governed protocol без собственной привязки отвергаются `Kernel._claim` без pack admission, ручной `validate_start`, `domain.bind`, `analysis.apply` и `batch.plan`.
3. **Закреплённые bytes.** Новый protocol вне пакетного пути отвергается, если его `implementation`, `data` или digest split совпадает с bytes, закреплёнными любой `pack_binding`: программами, input или captured artifacts. Это закрывает перерегистрацию закреплённых bytes legacy-корнем. Protocols, созданные до появления привязки (например, legacy protocol пилота ADR 0015), правило не затрагивает.

При чтении claim на pack-governed protocol без `pack_analysis` проваливает mechanical gate: «claim on a pack-bound protocol lineage lacks pack.analyse admission». Это отказ gate, а не replay. Истории `da6aa2a`, где такой путь уже использован, остаются проверяемыми Graph и backup, но успеха получить не могут. Правило уточнения шага 4 ADR 0016 («на pack-bound protocol `kernel.claim` и ручной `kernel.start_run` отвергаются») распространяется на всю линию.

### 6. Tripwires A-08 и A-09: что обнаруживается дёшево, а что остаётся ограничением

| Сигнал | Обнаружение и правило | Остаётся ограничением |
| --- | --- | --- |
| Реанализ скопировал digests оригинала (A-08) | Реестр и paper помечают `outputs_provenance=caller_declared` и `metrics_artifact_shared_with_original`. Confirmatory claim допускает только managed evidence, иначе gate отказывает. Совпадение digests само по себе **не** отказ: в golden `legacy_demo` честный реанализ seed 73 дал побайтно тот же `metrics.json`, что primary, а во всех managed runs `manual_binding` и `model_path` metrics primary и реанализа совпадают побайтно. | Caller-declared реанализ с изменёнными bytes неотличим от честного. Managed run не доказывает, что программа вычисляла, а не копировала (A-12). Настоящая проверка — пересчёт закреплённым доменным кодом (`recompute_metrics` пакета), а у legacy protocols его нет. |
| Confirmatory runs читают просмотренные bytes (A-09) | Для confirmatory claim gate отказывает, если `protocol.data` или `raw_data` любого run из evidence входит в snapshot `seen_data` этого protocol. | Копия с другими bytes, но той же информацией — переставленные строки, иная сериализация JSON, сжатие, смена единиц — не обнаруживается. |
| Перекодированная копия просмотренного split (A-09) | Используется «нестрогий digest»: SHA-256 после удаления UTF-8 BOM, замены CRLF на LF и обрезки пробелов в конце строк и файла; для bytes не в UTF-8 — исходный digest. При preregistration и при gate confirmatory claim нестрогий digest confirmatory split сравнивается с нестрогими digests `seen_data`, и совпадение отвергается. PoC аудита (`+ b"\n"`) этим ловится. | Всё, что выходит за эту нормализацию. Канонический fingerprint содержания — будущий hook DomainPack и отдельное решение к ADR 0016. Формулировку README:172 нужно уточнить: «byte-identical или отличающиеся только пробелами и переводами строк». |
| Повтор seed до успеха (A-07) | §4.5 | Для legacy protocols — только раскрытие. Повтор через новый protocol на тех же bytes виден в реестре семейства. |

### 7. Совместимость

#### 7.1 Решение: одна семантика для всех историй

Правила применяются при чтении ко всем историям; версии правил на store нет. Golden expectations меняются явно, в тех commits, где меняется наблюдаемое значение. Почему так:

1. Версия правил оставила бы eligible papers и approvals эпохи `da6aa2a`, построенные на неназначенном review. Это прямо противоречит контракту («never turn … unresolved findings into success»), а честность важнее стабильности.
2. Payloads и hashes существующих событий и рецепты basis не меняются. Поэтому проверка истории — Graph, replay, backup и restore — остаётся верной, а меняются только производные решения.
3. Golden test существует как раз для того, чтобы такие изменения были явными. Expectations обновляются в commit изменения со ссылкой на этот ADR. Сохранённые истории не перегенерируются, и `generate.py` не запускается.

#### 7.2 Что не меняется

- Payloads и hashes существующих kinds событий.
- Рецепты basis v1 и v2: `_local_evidence`, `_basis`, `_context_findings`, `protocol_exposures`.
- Структура gate и `checks_version`. По прецеденту ADR 0013 и 0016 версия не повышается: новые проверки только добавляют отказы.
- Replay исторических receipts.
- Список actions и их signatures.
- Множества nodes и edges Graph для историй без новых schemas событий.
- Код manifest v1, который использует replay.

#### 7.3 Существующие хранилища

| Хранилище | Что изменится | Что не изменится |
| --- | --- | --- |
| golden `legacy_demo` (26 events, reviews нет) | Bundle v2: альтернативный protocol с теми же hypotheses — связанная регистрация; 6 runs в реестре, все `caller_declared`; у реанализа seed 73 общий metrics artifact. Paper для demo claim теперь отвергается; в истории paper нет. | basis, gate, `next_action=scientific_review`, Graph, receipts, inventory |
| golden `manual_binding`, `model_path` | Bundle v2: анализ v1 помечен `caller_submitted_unverified`; assignment v1 полон для семейства из одного protocol. Approval по нему потребует пересчёта адаптера при submit; golden уже фиксирует совпадение `reproposal_digest`. | basis, gate, `next_action`, Graph, состояния batch и анализа |
| реальный пилот ADR 0015 (`.research/afterlife-pilot-20261004`, 124 events, 59 receipts по validation.md) | Bundle v2. Approval потребует пересчёта legacy-адаптера `afterlife_seed_v1` при submit. Его bytes должны совпасть с pin `domain.bind`, иначе отказ. | Семейство из одного protocol; basis `e0b7…`, gate, `next_action`, `awaiting_review`, Graph |
| копия с пакетным путём (`.research/domain-pack-real-state-20261004/pilot-pack-2/pilot-pack-copy`) | Legacy и pack protocols имеют одинаковый `input.dat` и поэтому образуют одно семейство. Assignment v1 claim пакета не полон: в его manifest нет 18 legacy runs. `analysis status` пакетного batch меняется с `awaiting_review` на `awaiting_assignment`. Отрицательное мнение о любом из двух claims заблокирует оба. | basis, gates |
| `.research/domain-pack-real-state-20261004/fresh-synthetic` | Только bundle v2 | Всё остальное |
| Истории с legacy approvals, papers `da6aa2a` или resolutions v1 (в репозитории — только тестовые fixtures) | Approvals становятся advisory; `next_action` — `scientific_review` или `replan`. Papers получают статус `not_eligible_under_current_rules`, и `materialize` отказывает. Resolutions v1 получают `not_admissible`. | Сами события и receipts |
| Истории с claims в стиле A-03 | Отказ gate | Graph и backup |
| Истории с событиями в стиле A-10 | Отказывают Graph, export, backup и решения — как для других поддельных событий | — |

Строки про `.research` — прогноз по коду и validation.md. Шаг 10 проверяет его на копиях, восстановленных во временный каталог; исходные каталоги на запись не открываются.

#### 7.4 Ожидаемые изменения golden

Меняется только `review_bundle_sha256` трёх stores, на шаге 8 (bundle v2). Остальные наблюдаемые значения после каждого шага равны прежним, и каждый commit это проверяет. В `tests/fixtures/golden/expected.json` записываются три новых значения и поле `revisions: [{commit, adr: "0018", fields: ["review_bundle_sha256"], reason}]`; истории `*.json` не меняются. Если на каком-либо шаге golden изменится сверх этого, commit останавливается до отдельного решения.

### 8. План тестов

Каждый PoC аудита становится регрессионным тестом.

- Сценарий теста строится только через API, существующие в `da6aa2a`, как в PoC; утверждения проверяют исправленное поведение.
- Перед commit новый тест запускается на снимке родительского commit, полученном `git archive`, с подложенным тестовым файлом. Тест обязан упасть на утверждении, а не на импорте, и пройти после commit. Оба результата записываются в validation.md.
- Утверждения, которым нужны новые API (ответ v2, `analysis verify`), проверяются только после commit. На старом снимке в этих тестах проверяется лишь то, что старый путь даёт успех.
- Две истории эпохи `da6aa2a` создаются кодом снимка `da6aa2a` и сохраняются в формате golden в `tests/fixtures/adr0018/`:
  - `forged_v1_analysis.json` — PoC A-04;
  - `legacy_review_paper.json` — PoC A-06: CLI approve и paper из demo.

  Это synthetic fixtures. Мнения в них явно помечены как fixture, а результат атаки нужен для проверки того, что он больше не eligible.

| Находка | Тест (файл) | Утверждение после исправления |
| --- | --- | --- |
| A-01 | `test_resubmitted_claim_inherits_family_veto_and_obligations`, `test_reregistered_protocol_on_same_bytes_joins_the_family` (`tests/test_review_families.py`) | B получает `replan`, paper отвергнут, veto и obligations A названы в ответе |
| A-02 | `test_resolution_applies_only_to_the_claim_it_evaluated` (`test_review_families.py`); обновлённый `test_descendant_protocol_paper_keeps_ancestral_review_lineage` | C2 получает `replan`; lineage paper отвергает чужую resolution; потомку нужна своя resolution |
| A-03 | `test_amendment_of_pack_bound_protocol_is_rejected`, `test_followup_of_pack_bound_claim_is_rejected`, `test_legacy_root_cannot_reuse_pack_pinned_bytes`, `test_stray_claim_on_pack_lineage_fails_gate` (`tests/test_pack_lineage.py`) | Отказ без событий; claim, записанный через `Store.append`, как его допускал `da6aa2a`, проваливает gate, а Graph строится |
| A-04 | `test_analysis_apply_rejects_a_proposal_the_adapter_did_not_compute`, `test_unverified_v1_analysis_needs_recomputation_before_approval` (`tests/test_batch_analysis.py`) | Подделка `supports` отвергнута; approval по истории `forged_v1_analysis` отвергнут при submit; честный путь не изменился |
| A-05 | `test_foreign_attempt_on_same_bytes_reaches_blind_bundle_and_paper` (typed и legacy), `test_unknown_attempt_of_identical_pack_protocol_is_disclosed`, `test_family_attempt_after_approval_stales_it` (`test_review_families.py`) | Попытка есть в manifest v2 и paper; approval перестаёт быть свежим |
| A-06 | `test_cli_review_cannot_approve_or_lift_another_reviewers_veto`, `test_demo_claim_paper_is_refused`, `test_legacy_review_paper_is_not_eligible` (`tests/test_cli.py`); `test_kernel_review_command_rejects_approve` (`tests/test_commands_service.py`) | Код 2 без событий; `replan`; paper отвергнут; paper из `legacy_review_paper` — `not_eligible_under_current_rules` |
| A-07 | `test_failed_attempt_metric_and_retried_seed_appear_in_paper`, `test_fixed_sample_retry_after_observed_outcome_fails_gate` (`tests/test_reporting.py`, `tests/test_kernel.py`) | В paper есть «-5.0» и повтор seed 7; в typed варианте gate отказывает |
| A-08 | `test_caller_declared_copied_reanalysis_is_labelled_and_cannot_be_confirmatory` (`tests/test_scientific_workflow.py`), `test_honest_identical_metrics_are_not_flagged` (`tests/test_golden_history.py`) | Метки в реестре и paper; confirmatory с unmanaged runs отвергнут; gate golden-реанализа seed 73 проходит |
| A-09 | `test_confirmatory_claim_on_exposed_inputs_fails_gate`, `test_whitespace_reencoded_copy_is_not_a_fresh_holdout` (`test_scientific_workflow.py`) | Отказ gate; отказ preregistration |
| A-10 | `test_raw_executor_approval_is_rejected_by_graph_and_decisions`, `test_raw_reviewer_approval_is_advisory` (`test_review_families.py`) | Graph и решения отказывают; `next_action` не `paper_candidate` |
| A-14 | `test_blind_v2_lists_linked_open_findings_for_acknowledgement`, `test_reassignment_after_unsubmitted_response`, `test_unsubmitted_negative_response_vetoes` (`tests/test_review_assignment.py`) | Approval допускается; неотправленный отрицательный ответ работает как veto |
| A-22 | `test_noncanonical_actor_ids_are_rejected` (`test_commands_service.py`), `test_historical_noncanonical_reviewer_cannot_approve` (`tests/test_pack_workflow.py`) | Пробел, гомоглиф и верхний регистр отвергнуты |

Новые пути требуют собственных тестов поведения:

- reconsideration снимает veto только для своего claim, а неполный список отзывов отвергается;
- resolutions `discriminating_experiment` и `narrow_claim` действуют для своих claims;
- replay, Graph и backup/restore сохраняют receipts v2;
- `analysis verify` даёт `matched` и `mismatched`.

Тесты, которые сейчас доводят claim до `paper_candidate` через legacy approval, переводятся на тестовый путь `review.assign` → выдача → `review.submit` (шаг 1). Положительные ответы в тестах остаются явно помеченными synthetic fixtures.

### 9. План реализации

Один проверенный шаг — один commit. Каждый commit содержит тесты и обновления документации и проходит:

- полный `python -m unittest discover -s tests -v`;
- CLI integration checks для затронутых интерфейсов;
- golden test, подтверждающий ожидаемые изменения из таблицы и отсутствие других.

Объёмы — грубая оценка изменённых строк.

| Шаг | Содержание | Тесты | Golden | Объём (src / tests / docs) |
| --- | --- | --- | --- | --- |
| 1. Тестовый путь review | `tests/review_paths.py`: assign → `ReviewerController` с fixture provider → submit. Перевод тестов, которым approval нужен для `paper_candidate` или paper. Семантика не меняется. | Все прежние проходят | нет | 0 / +250 −150 / 0 |
| 2. Семейства (A-01, A-02) | `review_admission.py`: `claim_family` и ядро решения; `next_action`, PaperBuilder и lineage работают по семейству; resolution — по паре (obligation, claim); guard `link_claims`. Переходное правило до шага 7: veto владельца снимает любое его позднее approval `c`. | A-01, A-02, обновление теста потомка | нет | +300 −40 / +250 / +40 |
| 3. Линия пакета (A-03) | `pack_lineage`; отказы в `_preregister`, `_claim`, `validate_start`, `domain.bind`, `batch.plan`; правило закреплённых bytes; отказ gate для stray claims | A-03, путь через `followup.apply` | нет | +110 / +180 / +25 |
| 4. Пересчёт в `analysis.apply` (A-04) | Реестр legacy-адаптеров; пересчёт и сверка; `batch_analysis` schema 2; replay v1 и v2; Graph | A-04; честный путь; дрейф исходника | нет | +160 −20 / +140 / +25 |
| 5. Actor IDs (A-22) | Канонический вид и ключ сравнения во всех проверках независимости и в CLI | A-22; около 10 строк тестов с IDs в верхнем регистре | нет | +90 −20 / +110 / +20 |
| 6. Reconsideration (аддитивно) | Ответ v2 и его schema; `veto_reconsideration_v1`; выбор policy; повторное назначение; submission и resolution schema 2; Graph | Новые пути §3.5, §3.6 | нет | +420 / +330 / +60 |
| 7. Обязательный допуск (A-06, A-10) | Предикат §3.1 в решениях; снятие veto только отзывом; отказ legacy approvals при записи; статус `not_admissible` для resolutions v1; `analysis_verification`; Graph отвергает невозможные reviews; отказ для demo; `paper_status` | A-06, A-10, A-04 (исторический v1); перевод тестов resolution и CLI на v2 | нет (в golden reviews нет) | +300 −80 / +300 −120 / +80 |
| 8. Реестр и policy v2 (A-05, A-07, A-14) | `attempt_ledger`; manifest v2 вместе с решением 4 ADR 0016; свежесть реестра; acknowledgement v3; paper и export (bundle v2); `_run_table`; повтор seed; `analysis status` и controllers создают assignment v2 | A-05, A-07, A-14 | `review_bundle_sha256` ×3 | +480 −40 / +320 / +70 |
| 9. Tripwires (A-08, A-09) | Confirmatory: просмотренные inputs и raw, только managed evidence, нестрогий digest | A-08, A-09; отсутствие ложного срабатывания на seed 73; обновление confirmatory тестов | нет | +130 / +220 −30 / +40 |
| 10. Проверка и документы | `episteme analysis verify`; проверка копий пилота во временном каталоге; итоговые README, architecture, mvp-plan, command-api, validation | CLI verify | нет | +70 / +60 / +100 |

Итого примерно +2 100 строк кода, +2 200 строк тестов и +500 строк документации. По правилам оценок MVP-плана это 8–11 инженерных дней. Рискованнее всего шаги 6–8: они меняют replay review receipts и затрагивают `graph.py`, который параллельно меняет ADR 0017.

Документы по шагам:

- шаг 2 — README:147 и :176, architecture §6 (veto, абзац ADR 0010), repository-map (новый модуль), ссылки на этот ADR из ADR 0003, 0009 и 0010;
- шаг 3 — ADR 0016, правило шага 4;
- шаг 4 — ADR 0013:16;
- шаг 7 — README:142–153, architecture §7 (legacy review commands), command-api (`kernel.review`, `review.submit`, `replanning.resolve_obligation`), ADR 0011, ADR 0012, ADR 0016:211;
- шаг 8 — ADR 0002:27, ADR 0011 (policy v2), ADR 0016 (шаг 7), README:163 и :173;
- шаг 9 — README:172 и ADR 0002.

После каждого шага раздел «Ход реализации» этого ADR фиксирует состояние, а принятие ADR остаётся за координатором.

### 10. Связь с ADR 0017

- **Общие файлы.** ADR 0017 меняет `store.py` (проверенный снимок, read scope CAS), `graph.py` и `cli.py`. Этот ADR меняет `graph.py` (§3.3, новые schemas) и `cli.py` (`review`, `analysis verify`, флаги actors, реестр адаптеров). Поэтому шаги 3–10 делаются поверх зафиксированного ADR 0017. `store.py` этот ADR не трогает: проверка канонических IDs стоит в handler-пути `CommandService`, после fast path replay.
- **Стоимость.** При каждом `next_action` для claim с reviews проекция допуска проигрывает индексы submission, delivery, assignment и resolution и строит семейство и реестр. ADR 0017 убирает повторное хэширование событий, receipts и CAS, но производные индексы по-прежнему пересчитываются при каждом вызове: «инкрементального сопровождения индексов между транзакциями нет». Поэтому проекция строится один раз на снимок и переиспользуется для всех claims в PaperBuilder, export и `analysis status`. Шаг 8 измеряет её стоимость счётчиками `Store.verification_counts` ADR 0017 на копии пилота.
- **Один снимок.** События и receipts проекции должны относиться к одной ревизии; это связано с A-17, где Graph читал receipts вне своего снимка. Проекция использует семантику `_verified_receipts(history)` из ADR 0017. Если она не даёт согласованной пары событий и receipts, это предпосылка уровня хранилища, а не часть этого ADR. **Отклонение (шаг 0):** проверка показала, что `_verified_receipts(history)` для более старого снимка отказывает ложно, поэтому проекции берут `store.receipts()` с фильтром по префиксу снимка (раздел «Ход реализации»).
- **Номера строк** здесь относятся к `da6aa2a`. После ADR 0017 строки `cli.py` и `graph.py` сдвинутся, и ориентироваться нужно на функции.
- **Golden.** ADR 0017 заявляет, что производные hashes golden не изменились. Изменение шага 8 отсчитывается от того же baseline.

### 11. Связанные находки вне этого ADR

- **Хранилище** (пересматривается после ADR 0017): A-13 (`INSERT OR REPLACE` в обход triggers), A-15 (откат при restore старого снимка), A-16 (нейтрализованные triggers), A-18 (дубли ключей в тексте payload), A-19 (`inspect` и `gate` без replay; поддельные receipts в `Store.receipts()`). Гарантии этого ADR действуют только для истории, которую хранилище сохраняет верно: владелец файлов (A-13) или программа исполнителя (A-12) может удалить отрицательный review.
- **Исполнение и пакеты** (будущий ADR профиля исполнения): A-11, A-12, A-23. Пересчёт адаптера (§5.1) и hooks пакетов исполняются в процессе ядра, поэтому злонамеренный код может подделать и пересчёт.
- **Надёжность:** A-17 (связан с ADR 0017), A-20, A-21.
- **ADR 0016:** шаг 7 (отображение report в отчётах и подписи roster; часть про policy переходит сюда), шаг 8 (третий пакет: его положительная confirmatory ветвь согласуется с требованием managed evidence), шаг 9 (модельный путь).
- **Будущие решения, на которые ссылается этот ADR:** запись утраченной попытки; meta-review для исключений из семейства; resolutions остальных видов findings; fingerprint содержания данных в пакете; порционная выдача больших семейств; аутентифицированные назначения (M4).

### 12. Ограничения

- **Идентичность.** IDs и роли по-прежнему заявляет caller. Caller, которому доступны строки planner и reviewer, может пройти всю цепочку сам: `review.finalize` принимает любой CAS digest (ADR 0012). Этот ADR делает цепочку обязательной и видимой, но не аутентифицирует её участников.
- **Reconsideration** защищает честную stateless-сессию под ID владельца: до отзыва она видит собственные замечания. От подмены владельца она не защищает.
- **Избыточная блокировка.** Семейство по общим bytes блокирует и независимые вопросы на общих данных, пока владелец veto не отзовёт его для каждого claim.
- **Обход семейства.** Новые bytes вместе с новыми hypothesis IDs выводят попытку из семейства. Связанные регистрации ловят только тот же вопрос или тот же набор гипотез, а сходство текста не используется.
- **Неразрешимые obligations.** Obligations видов, для которых нет resolution, блокируют семейство до появления новых переходов.
- **Незавершённые попытки** раскрываются, но не блокируют paper.
- **Слепота — это спецификация.** Reviewer под той же OS identity может прочитать Store (ADR 0011, 0012).
- **Недоступность исходного reviewer.** Если исходный reviewer становится недоступен, его veto или открытые obligations могут бессрочно блокировать всё семейство claims: снять их может только он сам, а политики замены reviewer пока нет. Это будущая работа. Любая такая политика должна быть явной, записываться в журнал и быть видимой в paper; молчаливое истечение veto или замена владельца по умолчанию не допускаются.
- **Механический допуск — не научная оценка.** `scientific_validity=not_assessed` не меняется. Допуск не устанавливает научную правильность review или resolution.

### 13. Спорные решения

1. **Семейство по общим bytes.** Альтернатива — только линия protocols плюс обязательная явная связь `supersedes` или `limits` для нового claim рядом с открытым замечанием, как предлагал аудит. Выбраны bytes, потому что перерегистрация на тех же данных — тот же обход (A-05). Цена — блокировка независимых вопросов на общих данных.
2. **Асимметрия допуска.** Отрицательные мнения блокируют без assignment и receipt, а approvals засчитываются только через полную цепочку. Буквальная симметрия ослабила бы veto: замечание без provenance исчезало бы.
3. **Veto снимается только явным отзывом под `veto_reconsideration_v1`.** Альтернатива — любое позднее approval владельца, как в ADR 0003. Это проще, но stateless-сессия снимала бы veto, не увидев замечания.
4. **Legacy approvals отвергаются при записи, а исторические становятся advisory.** Альтернатива — продолжать запись и не засчитывать. Совместимость команд была бы выше, но журнал копил бы approvals, которые ничего не значат.
5. **Реестр связывается через manifest и submission, а рецепт basis не меняется.** Альтернатива — basis v3 с реестром. Он сразу инвалидировал бы все сохранённые bases, assignments и links и потребовал бы версии рецепта в Graph.
6. **Одна семантика для всех историй.** Альтернатива — версия правил на store, но она оставила бы eligible paper на неназначенном review.
7. **Незавершённые попытки не блокируют paper.** Альтернатива — блокировать до перехода «исход утрачен». Это честнее, но без такого перехода семейство закрыто навсегда.
8. **Связанные регистрации раскрываются, но не связывают решения.** Альтернатива — включить их в свежесть; тогда reviews в активной study будут постоянно устаревать.
9. **Confirmatory claim требует managed evidence.** Альтернатива — только раскрытие.
10. **Канонические ASCII IDs.** Альтернатива — Unicode с NFKC, casefold и таблицей confusables UTS #39, которой нет в stdlib.
11. **Blind v2 раскрывает открытые замечания связанных claims вне семейства.** Альтернатива — двухфазный review: сначала слепое мнение, затем acknowledgement.
12. **Policy v2 объединена с решением 4 ADR 0016.** Альтернатива — отдельные версии v2 и v3.
13. **Assignments v1 остаются годными, если полны для семейства.** Альтернатива — переназначить всё. Это изменило бы состояние golden и реального пилота без выигрыша в честности.
14. **`checks_version` не меняется** — по прецеденту ADR 0013 и 0016. Альтернатива — новая версия, которая изменила бы `gate_sha256` всех stores при неизменном результате.

## Ход реализации

ADR 0017 зафиксирован в `73281b5`, и шаги идут поверх него в порядке раздела 9. Каждый шаг — отдельный commit с тестами и обновлением документов. Статус исправления каждой находки ведётся в разделе «Статус исправлений» [аудита](../adversarial-audit-2026-10-04.md).

| Шаг | Состояние |
|---|---|
| 0. Принятие ADR и публикация аудита | Выполнен в `f6ad309`: аудит и этот ADR добавлены в репозиторий и связаны из README, architecture, mvp-plan и repository-map. Код не менялся. |
| 1. Тестовый путь review | Реализован и локально проверен в `ff4ef0f`: `tests/review_paths.py` проводит fixture-мнение через `review.assign` → `ReviewerController` с fixture provider → `review.submit`. Код ядра не менялся. Тесты, которым approval нужен для `paper_candidate`, `superseded` или paper, найдены временной имитацией правила шага 7 (approval засчитывается, только если на него ссылается `review_submission`; в commit не входит) и переведены на этот путь. Тесты, проверяющие отказы legacy-записи, остаются на legacy-пути до шага 7. |
| 2. Семейства (A-01, A-02) | Реализован и локально проверен в `2b3773e`: [`review_admission.py`](../../src/episteme/review_admission.py) вычисляет линию protocols и семейство claim по событиям снимка; `Kernel._next_action`, `PaperBuilder.build` и `materialize` решают по семейству; resolution засчитывается только для пары (obligation, `resolution.claim`); lineage paper отвергает resolution другого claim; guard `link_claims` проверяет все затронутые семейства. Тесты `tests/test_review_families.py` (A-01 в двух вариантах, перерегистрация на тех же bytes, A-02) и обновлённый тест lineage потомка падают на `ff4ef0f` и проходят после шага. |
| 3. Линия пакета (A-03) | Реализован и локально проверен в `c53f3f5`: `pack_lineage` и `pinned_bytes` в `domain_packs.py`; `Kernel._preregister` отвергает amendment из pack-governed линии (закрывает `kernel.preregister`, `kernel.preregister_for_set` и `followup.apply`) и protocol вне пакетного пути с закреплёнными bytes; `kernel.claim`, ручной `kernel.start_run`, `domain.bind`, `analysis.apply` и `batch.plan` отвергаются на потомках; claim без `pack_analysis` на линии проваливает gate. Тесты `tests/test_pack_lineage.py` падают на `2b3773e` и проходят после шага. |
| 4. Пересчёт в `analysis.apply` (A-04) | Реализован и локально проверен в `d2baa00`: реестр `LEGACY_ANALYSIS_ADAPTERS` и `legacy_analysis_adapter` в `domains/registry.py` (CLI берёт адаптер оттуда же); `analysis.apply` сверяет ID, версию и digest исходника и допускает только proposal, равный пересчёту; `batch_analysis` schema 2 с `proposal_origin`; replay принимает schema 1 и 2; `analysis_provenance` даёт производную метку. Тест `test_analysis_apply_rejects_a_proposal_the_adapter_did_not_compute` падает на `c53f3f5` и проходит после шага. Требование пересчёта при approval исторического анализа schema 1 — шаг 7. |
| 5. Actor IDs (A-22) | Реализован и локально проверен в `67f91bd`: `canonical_actor`, `actor_key` и `independent_of` в `kernel.py`. Канонический вид проверяют `CommandService` (actor контекста и поля `reviewer_actor`, `executor`, `replicator`, после fast path replay), `Kernel._write`, controllers анализа и флаги CLI `--actor`, `--planner`, `--analyst`, `--reviewer`. Независимость на новых записях сравнивается по ключу в `_record_review`, guard `link_claims`, `review.assign`, `review.submit`, `replanning.resolve_obligation`, `analysis.apply` и `pack.analyse`. Тесты `test_noncanonical_actor_ids_are_rejected` и `test_noncanonical_reviewer_variants_are_rejected` падают на `d2baa00` и проходят после шага. |
| 6. Пересмотр veto (§3.5, §3.6) | Реализован и локально проверен в `e91527f`: `review_assignment.select_policy` выбирает `veto_reconsideration_v1`, если собственные отрицательные мнения назначаемого reviewer или его obligations, не разрешённые для `c`, связывают семейство; assignment schema 2 хранит `policy` и `projection`, manifest получает `own_findings`. Повторное назначение разрешено, пока ни одно назначение с тем же ключом не получило submission. Завершённый неотправленный отрицательный ответ считается veto. Ответ v2 ([schema](../../schemas/review-response-v2.schema.json)) обязателен под reconsideration и запрещён вне его. `review.submit` пишет submission schema 2 (`policy`, `withdrawals`, `resolutions`, `family_ledger_digest`, `analysis_verification`) и resolutions schema 2 видов `discriminating_experiment` (claim на потомке follow-up) и `narrow_claim`. Withdrawal снимает veto только для пары (мнение, claim). Тесты A-14 (`test_unsubmitted_negative_response_vetoes`, `test_reassignment_after_unsubmitted_response`) и `ReconsiderationTests` падают на `67f91bd` и проходят после шага. |
| 7. Обязательный допуск (A-06, A-10) | Реализован и локально проверен в `5251c09`: `Admission.approval_defect` применяет предикат §3.1 в `next_action`, PaperBuilder и lineage. Approval засчитывается только из проверенной цепочки `review.submit`, от канонического reviewer, независимого по ключу от contributors своего префикса, с полной проекцией v1 по runs семейства, с `recomputed_match` для непроверенного анализа schema 1 и (для submission schema 1) без открытых собственных мнений. Остальные approvals выводятся как `advisory_approvals` с причиной. Veto снимает только явный withdrawal. `kernel.review`, `kernel.review_with_links` и CLI `review` отвергают `approve`; `replanning.resolve_obligation` больше не пишет resolutions, а исторические resolutions v1 имеют статус `not_admissible`. Graph и проекция допуска отвергают review с ролью не `reviewer` или от contributor. PaperBuilder отвергает synthetic demo claims; `inspect` и `materialize` используют `paper_status`. Новые тесты (A-06, A-10, исторические A-04 и A-22) падают на `e91527f` и проходят после шага. |
| 8. Реестр и policy v2 (A-05, A-07, A-14) | Реализован и локально проверен: `review_admission.attempt_ledger` строит реестр всех попыток семейства и связанных регистраций (§4.1) с `family_ledger_digest` и `disclosures`. `review.assign` пишет manifest `blind_initial_review_v2` (reconsideration — `projection=blind_initial_review_v2`) с разделами `family`, `related_registrations`, `linked_open_findings`, `pack_reports` и `analysis_provenance`; replay использует записанную `projection`. Approval засчитывается, только если digest реестра на префиксе назначения равен текущему; `review.submit` такое approval отвергает. Acknowledgement v3 (`review_schema_version=3`) требует только `linked_open_findings`; Graph принимает версии 1–3. Paper получает разделы реестра, связанных регистраций и допуска reviews, строки «Kernel disclosure» в limitations и метрику с причиной для failed и cancelled в `_run_table`; review bundle и bundle paper — `bundle_version=2`. `paper_status` различает `current`, `historical` и `not_eligible_under_current_rules`. Gate typed `fixed_sample` отказывает при повторе seed после записанного исхода. Новые тесты падают на `5251c09` (8 отказов на assertions) и проходят после шага. |
| 9–10 | Не начаты. |

### Уточнения шага 8

- **Свежесть сравнивается с префиксом назначения.** Approval не засчитывается, если `family_ledger_digest` на префиксе его assignment отличается от текущего. Так reviewer заведомо видел именно этот реестр. Submission schema 2 уже записывает тот же digest с шага 6.
- **Acknowledgement v3 применяется только к назначениям с projection v2 и только при наличии `link_assessments`.** Assignments v1 и reconsideration без projection v2 воспроизводятся и отправляются по прежнему правилу schema 2, чтобы сохранённые receipts совпадали.
- **Отклонение: размещение тестов.** Раздел тестов называл `tests/test_reporting.py`, `tests/test_kernel.py` и `tests/test_review_assignment.py`. Тесты A-07 находятся в `tests/test_scientific_workflow.py`, а A-14 — в `tests/test_claim_workflow.py`, потому что там уже есть фикстуры typed и legacy protocols и связанных ветвей claims.
- **Отклонение: незавершённые попытки проверены на ручном run.** Тест A-05 о попытках без исхода использует незавершённый ручной run (`incomplete_manual_run`), а не `unknown`/`queued` пакетного пути. Статусы `unknown` и `queued` вычисляются той же функцией по `execution_job` и его dispatch, но отдельного регрессионного теста для них нет.
- **Отклонение: ревизия golden без собственного hash.** В `tests/fixtures/golden/expected.json` три значения `review_bundle_sha256` обновлены (bundle v2) и добавлен раздел `revisions` с `adr`, `step=8`, `parent_commit=5251c09` и списком полей. Hash самого шага нельзя записать в файл, входящий в этот commit. Histories, bases, gates, Graph, receipts и inventories golden не изменились.
- **Связанные регистрации — по общим hypotheses.** Ребро `shared_hypotheses` соединяет с семейством protocols вне его, у которых есть хотя бы одна общая hypothesis. В `legacy_demo` так раскрывается альтернативный protocol; на свежесть он не влияет (решение 8).

### Уточнения шага 7

- **Исторические сценарии — сохранённые истории `da6aa2a`.** Три хранилища в `tests/fixtures/adr0018/` (legacy approval demo claim и paper; approval через `review.submit` по подложному анализу schema 1; назначенный approval reviewer `Fixture-Executor`) созданы скриптом `tests/fixtures/adr0018/generate.py` на снимке `git archive da6aa2a` и проверяются в `tests/test_historical_admission.py`. **Отклонение:** раздел тестов называл `test_cli.py`, `test_batch_analysis.py` и `test_pack_workflow.py`; исторические тесты собраны в одном модуле, потому что требуют одинаковой загрузки истории, а сценарий A-22 построен на demo, а не на пакете: для предиката важен только ID reviewer.
- **Пересчёт исторического анализа разделён между шагами 6 и 7.** Проверка при записи (`review.submit` пересчитывает proposal и отказывает при расхождении) вошла в шаг 6. Шаг 7 добавил чтение: approval по непроверенному анализу schema 1 без записанного `recomputed_match` (submission schema 1 эпохи `da6aa2a`) не засчитывается.
- **Отклонение: manifest reconsideration включает все protocols семейства.** В `e91527f` manifest `veto_reconsideration_v1` перечислял runs только protocol самого claim, поэтому по предикату §3.1 (полная проекция для семейства) reconsideration claim потомка никогда не была бы допустима. Теперь manifest перечисляет protocols и runs всего семейства. Assignments reconsideration, записанные кодом `e91527f`, при replay дадут другой manifest и будут отвергнуты; такие истории есть только во временных каталогах тестов, в репозитории и в копиях пилотов их нет.
- **Правило acknowledgement v3 (`review_schema_version=3`) перенесено на шаг 8** вместе с проекцией `blind_initial_review_v2`: без `linked_open_findings` в manifest новое правило нечем выполнить. Graph по-прежнему применяет к reviews исходное правило acknowledgement.
- **Отказ Graph по contributor — по точной строке.** Для исторических событий Graph и проекция допуска считают невозможным review от contributor по точному совпадению ID: так история `da6aa2a` с неканоническим reviewer остаётся читаемой, а её approval получает причину «non-canonical historical reviewer ID». Сравнение по ключу действует для засчитывания approvals и для новых записей.
- **Paper demo.** Отказ `PaperBuilder.build` для `mode == "synthetic_demo"` проверяется после проверки eligibility, чтобы прежние сообщения об отказе не менялись. Поэтому CLI больше не может собрать успешный paper ни из одного встроенного сценария; materialization internal draft проверяют тесты `test_reporting`.
- **Перевод тестов.** Тесты, которые снимали veto поздним approval того же reviewer через legacy-путь, переведены на reconsideration с withdrawal или на отрицательные мнения. Исторические resolutions v1 в `tests/test_resolution.py` создаются через `patch` прежнего тела команды; их receipts воспроизводятся, а статус равен `not_admissible`.

### Уточнения шага 6

- **Отклонение: проекция reconsideration — v1 до шага 8.** Проекции `blind_initial_review_v2` ещё нет, поэтому manifest `veto_reconsideration_v1` строится как проекция `blind_initial_review_v1` плюс `own_findings`. Из исключений убрано `prior_review_verdicts` и добавлено `other_reviewers_verdicts_and_rationales`: собственные мнения reviewer видит, чужие — нет. Использованная проекция записана в поле `projection` assignment schema 2; replay восстанавливает manifest по нему. На шаге 8 новые назначения получат проекцию v2, а исторические сохранят v1.
- **Выбор policy проверяется при replay.** Для assignment schema 2 replay заново вычисляет выбор на историческом снимке и отвергает событие с другой policy. Assignment schema 1 воспроизводится как `blind_initial_review_v1`.
- **Тесты A-14 перенесены из шага 8 в шаг 6.** Правило §3.6 (повторное назначение и veto неотправленного ответа) понадобилось уже здесь: без него владелец veto не мог бы получить reconsideration на тот же basis. Тест прежнего запрета повторного назначения переписан под новое правило.
- **Неотправленный отрицательный ответ** становится veto своего `reviewer_actor` в момент, когда доставка завершена. Последующий approval того же reviewer под reconsideration снимает его только через явный withdrawal. Отклонённая попытка `review.submit` не стирает такой ответ: он остаётся veto.
- **Переходное правило шага 2 сохранено** для approvals без submission (legacy-путь): любой поздний approval владельца по `c` снимает его veto для `c`. Его отменяет шаг 7, когда approvals без `review_submission` перестанут засчитываться.
- **`family_ledger_digest` и `analysis_verification` записываются уже сейчас,** чтобы schema 2 submission не менялась на шагах 7–8. Ledger здесь — упорядоченный список пар (run, result) линии `K`; полный реестр попыток §4.1 (шаг 8) получит отдельный digest в manifest. `analysis_verification` равно `recomputed_match`, если approval касается claim, допущенного непроверенным `batch_analysis` schema 1: команда пересчитывает proposal адаптером (ID, версия, digest исходника, канонический proposal) и отказывает при расхождении; replay проверяет только, какое значение требовалось. Иначе значение `not_required`. Недопуск исторических approvals без такой проверки — шаг 7.
- **`review_schema_version=3` отложен до шага 7.** Review из submission schema 2 пока сохраняет прежний вид payload; правило acknowledgement Graph меняется вместе с допуском шага 7.
- **Replay без экспоненты.** `Admission(replay=True)` проверяет submissions, obligations и resolutions через их replay и используется для решений и новых записей. Внутри replay исторических receipts используется структурный вариант `replay=False`; иначе проверка каждого receipt повторно проверяла бы всю предшествующую историю.
- **Fixture-путь тестов.** `tests/review_paths.py` отправляет ответ v2, если assignment выбрал reconsideration: approval отзывает все открытые собственные мнения, отрицательный verdict передаёт пустые списки. Отказ ответа v1 под reconsideration проверяет отдельный тест.

### Уточнения шага 5

- **Replay сравнивает по точной строке.** Функции, общие для записи и replay (`review_assignment._manifest`, `review_submission._decision`, `resolution._admit`), получили флаг `keyed`: новые записи сравнивают ключи, replay — точные строки, как при историческом допуске. Иначе история эпохи `da6aa2a` с вариантом ID стала бы непроверяемой для Graph и backup. Approvals неканонических reviewers станут недопустимыми при чтении на шаге 7.
- **Владение veto — по точной строке.** Approval канонического ID не снимает veto исторического неканонического варианта: тождество этих ID нельзя проверить, и так строже. Поскольку неканонический ID больше не может писать, такое veto снимается только новым решением о замене reviewer, которого пока нет (раздел 12).
- **Гомоглифы.** Ключ NFKC и casefold не отождествляет кириллическую «а» с латинской `a`; такие ID отсекает только ASCII-форма новых записей (спорное решение 10).
- **Таблица «actor → роли»** в paper и export относится к шагу 8, где меняются paper и bundle.
- Тестовые ID с верхним регистром (`fixture-A-…`, `fixture-R1`, `reviewer-R1`, `executor-A`) приведены к нижнему регистру. Имя `test_historical_noncanonical_reviewer_cannot_approve` из §8 оставлено для шага 7: оно проверяет допуск исторического approval, а не отказ при записи.

### Уточнения шага 4

- **Replay не исполняет адаптер.** Пересчёт выполняется только внутри команды на её снимке. Replay schema 2 проверяет структуру и наличие `proposal_origin`, как replay `pack.analyse` проверяет envelopes без импорта пакета. Поэтому позднее изменение исходника адаптера не делает исторические события непроверяемыми; расхождение живого кода с записанным покажет read-only `analysis verify` (шаг 10).
- **Pin ручной привязки** по-прежнему проверяет `_origin`: для batch с `domain.bind` digest исходника обязан совпасть с pin. Для batch модельного пути ADR 0008 pin нет, и digest сверяется только с модулем зарегистрированного адаптера.
- **Golden.** Истории `manual_binding` и `model_path` содержат события schema 1; их replay и наблюдаемые значения не изменились.

### Уточнения шага 3

- **Отклонение: наличие привязки проверяется структурно.** §5.2 говорит о привязке, «проверенной replay». `pack_lineage` ищет событие `pack_binding` на цепочке `parent` без replay: `validate_start` намеренно не читает receipts внутри транзакции, а replay каждой привязки уже выполняют `pack_bindings` (`batch.plan`, `pack.analyse`), Graph, export и backup. Поддельная привязка без receipt может только добавить отказы, но не допустить claim, поэтому структурная проверка строже.
- **Пакетный путь.** `pack.preregister` создаёт protocol через `Kernel._preregister`. Правило закреплённых bytes для него не действует: команда вызывает приватный `_preregister_for_set(pack_path=True)`, а публичные `preregister` и `preregister_for_set` всегда идут с `pack_path=False`. Иначе второй protocol того же пакета был бы невозможен.
- **Только путь записи.** Для `domain.bind` и `analysis.apply` новая проверка стоит в обработчике команды, а не в общих с replay функциях. Так истории эпохи `da6aa2a` с такими событиями остаются проверяемыми Graph и backup.
- **Gate.** Отказ «claim on a pack-bound protocol lineage lacks pack.analyse admission» действует и для protocol с собственной привязкой: claim, дописанный через `Store.append`, раньше проходил gate и отвергался только replay в Graph.
- **Закреплённые bytes** — файлы кода пакета, программы primary и реанализа, input и захваченные artifacts (bundle и его inventory). Environment в набор не входит: один fingerprint среды законно разделяют разные protocols.

### Уточнения шага 2

- **Переходное правило.** До шага 7 veto владельца для claim `c` снимает любое его approval `c`, записанное позже отрицательного мнения; для мнения о самом `c` это прежнее правило «последнее мнение reviewer». Reviewers сравниваются по точной строке ID до шага 5.
- **Ответ `next_action`.** Если veto пришло через другой claim семейства, ответ получает ключ `family_vetoes` со списком `{review, claim, reviewer}`; `reasons` содержат actions всех действующих veto. В прежних ситуациях форма ответа не изменилась.
- **Obligations связанного контекста** сохраняют прежнее блокирующее действие (см. «Отклонение (шаг 2)» в §2).
- **`open_obligations`** в `replanning.py` сохраняет прежний смысл (у obligation нет действующей resolution ни для какого claim); им пользуются только тесты. Решения берут проекцию `review_admission`.
- **Стоимость.** Проекция `Admission` один раз на снимок проигрывает индексы obligations и resolutions и вычисляет семейства. Внутри `Store.reading()` или транзакции команды она кешируется в memo CAS этого scope по ключу (длина истории, голова цепочки) и исчезает вместе с ним. Вне scope каждый вызов строит её заново, но индексы в одном `_next_action` проигрываются один раз, а не для каждого исходного claim, как раньше. Receipts берутся `_index`-функциями через `store.receipts()` с фильтром по префиксу снимка. Измерение счётчиками ADR 0017 — шаг 8.
- **Тест lineage потомка** переименован в `test_descendant_protocol_paper_needs_its_own_resolution`: прежнее имя утверждало обратное. Теперь он проверяет `replan`, отказ paper и отказ lineage для resolution ребёнка.

### Уточнения шага 1

- Повторное approval того же reviewer на том же basis через путь assignment невозможно: `review.assign` отвергает второе назначение с тем же ключом (claim, basis, reviewer). Тест, где reviewer меняет оценку supersession, теперь записывает новую оценку на новой ревизии evidence (после failed-попытки). Два теста, где повторное approval проверяет рецепт `_context_findings` (он по §7.2 не меняется), пишут повтор через legacy-путь; шаг 7 переведёт их на событие в стиле исторической истории.
- Семь тестов, зависящих от legacy approvals, переводятся позже: четыре теста resolution v1 и тест lineage потомка (шаги 2, 6 и 7), CLI `review` и replay команды `kernel.review` (шаг 7).

### Проверка перед реализацией: A-17 и ADR 0017

Раздел 10 предполагал, что проекция допуска получит согласованную пару событий и receipts через `_verified_receipts(history)` ADR 0017. Проверка на `73281b5` это опровергла (`.research/adr0018-a17-check/a17_check.py`, вне репозитория). Если другой writer успевает записать команду между `store.events()` и `_verified_receipts(history)`, функция сверяет каждую строку receipts со старым снимком и отказывает: `invalid command receipt event range`. Так же отказывает `_Projection(store, history).build()` в Graph. Поэтому ADR 0017 находку A-17 не закрывает. Согласованную пару даёт другой приём, которым уже пользуются все `_index`-функции: `store.receipts()`, отфильтрованные по `after_revision <= len(history)`. При append-only истории снимок — префикс текущей цепочки, а receipts этого префикса проверены против тех же событий. Проекции этого ADR используют этот приём. A-17 остаётся находкой надёжности вне этого ADR.
