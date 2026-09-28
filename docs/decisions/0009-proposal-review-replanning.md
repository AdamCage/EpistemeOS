# ADR 0009: подготовка предложенного эксперимента и сохраняемый follow-up после review

Дата: 24 сентября 2026; проверено 28 сентября. Статус: локальный инкремент; полный автономный цикл и независимый scientific review не реализованы.

## Задача

После [применения модельного предложения](0008-experiment-proposals.md) в истории есть frozen protocol и экспериментальный узел, но нет выбранного задания. После отрицательного `kernel.review` есть текст `actions`, но нет адресуемых обязательств и связи с новым планом. Эти границы нужно пересекать сохранёнными командами, не подменяя план выполненным экспериментом или мнение reviewer научным фактом.

## Предложение → выбранный frozen batch

`proposal.prepare_next` — команда planner через `CommandService`. Она вызывает текущий `Search.select_next(tree)` и принимает только выигравший узел, который однозначно создан применённым `agent.apply_experiment` schema v2. Из исходной immutable compilation и protocol берутся reanalysis source, environment и output contract. Затем `Batch.plan` фиксирует назначения executor/replicator и полный roster `primary`/`reanalysis` для каждого seed. `search_selection` и `batch_plan` появляются подряд в **одной** command receipt; историческая проверка связывает их с исходным request/application и study. Возможности, формат environment и source bytes снова проверяются при admission batch.

Если frontier даёт wait/stop, выигрывает ручной узел, proposal устарел, не хватает бюджета или batch не проходит проверки, вся транзакция откатывается. Этот bridge не перескакивает через более приоритетный узел и не сохраняет ложный выбор. Результат команды — ID batch; повторная доставка исходного envelope возвращает историческое подтверждение, которое само по себе не говорит, что batch до сих пор готов к запуску. Команда ничего не запускает и не создаёт run, evidence, claim, review или paper. Дальше нужны отдельный `batch advance`, анализ и gates.

Стоимость первой policy — `2 × число seeds` в `enqueued_attempt`. Это резерв числа возможных attempts, а не предел CPU, токенов или денег. У primary и повторного анализа разные source digests и назначенные actor IDs, но оба читают одну исходную выборку, а идентификаторы не удостоверяют авторство или независимость контекста.

## Отрицательное мнение → typed obligations

`replanning.record_review` вызывается reviewer через `CommandService` на текущем `expected_basis`. Он повторно проверяет mechanical gate и контекст, применяет действующие правила `Kernel.review`/`review_with_links`, а затем в той же транзакции добавляет по одному `review_obligation` на каждый typed finding. Допускаются только `request_changes` и `reject`. В каждом finding есть `kind`, текст `action`, проверяемый в будущем `closure_criterion` и непустые `evidence_refs` на допустимые event IDs текущего review context. В обязательстве сохраняются hash исходных claim/review, `basis_hash`, индекс finding и hash каждой cited revision. Исходное отрицательное мнение и исходная preregistration остаются неизменными.

Текущие виды finding: `discriminating_experiment`, `independent_reanalysis`, `narrow_claim`, `request_data`, `stop_inconclusive`. Это тип действия, запрошенного reviewer, **не** verdict о достаточности evidence. `open_obligations` возвращает сохраняемые открытые записи; позднее положительное мнение или создание эксперимента не закрывают их. Проверка полной исходной receipt при replay отличает подлинную пару review/obligations от свободного текста в отдельном event.

Механический допуск означает только соответствие зарегистрированному evidence basis и rules ядра. Reviewer ID задаёт доверенный локальный caller; role check и отличие от contributor IDs не доказывают независимое рассуждение, отдельную модельную сессию, аутентификацию или файловую/сетевую изоляцию. Scientific correctness finding не устанавливается автоматически.

## Obligation → дочерний план

`followup.apply` — отдельная команда planner для одного открытого `discriminating_experiment` obligation. Она повторно сверяет, что исходный отрицательный review остаётся последним мнением этого reviewer по claim, evidence basis не изменился и gate проходит. Исходный эксперимент должен быть завершённым научным узлом дерева; новый ExplanationSet должен иметь актуальную ревизию, тот же study и scope. Planner подаёт полный `protocol_spec` и `node_spec`, включая научное обоснование. Команда проверяет capacity дерева и бюджет `enqueued_attempt`, стоимость полного primary/reanalysis roster и неизменяемость parent protocol.

В одной receipt создаются `protocol`, дочерний `experiment_node` и `replan_followup`. Последнее событие связывает точные hashes obligation, review, claim, parent, tree, ExplanationSet, нового protocol/node и сохранённый specification artifact. Повторное применение того же obligation не допускается. `followup_state` показывает `planned` и ID новых записей, но `obligation_resolution` остаётся `open`, а `scientific_validity` — `not_assessed`. Новый узел ещё не выбран, не зарезервирован в batch, не исполнен и не проверен reviewer.

## Дочерний план → выбранный frozen batch

`followup.prepare_next` — planner-команда для записанного obligation. Она повторно проверяет исходный negative review как последнее мнение того же reviewer, текущий mechanical evidence basis и актуальную planning lineage. `Search.select_next` должен выбрать именно дочерний узел указанного `replan_followup`; более приоритетный другой узел не пропускается. Planner явно передаёт `reanalysis_implementation`, `reanalysis_environment`, output contract, лимиты и назначенных executor/replicator: исходный `followup.apply` не сохранял runnable recipe. `Batch.plan` сверяет frozen source bytes, локальный environment, разные implementation digests и полный `primary`/`independent_reanalysis` roster.

`search_selection` и `batch_plan` фиксируются подряд в одной receipt с action `followup.prepare_next`. Ошибка выбора, stale basis или batch gate откатывает оба события. Replay проверяет точный исходный prefix, решение best-first policy, request recipe и study, не используя поздний scientific verdict для пересмотра исторического выбора. Перед каждой новой записью `batch.enqueue_slot` и `execution.dispatch` для такого batch source review/basis и planning lineage проверяются снова. Если они изменились после reserve, исторический batch остаётся в истории, но новый job или запуск worker запрещён; уже записанный dispatch только reconcile, без второго запуска. Отдельный `batch advance` нужен для технического исполнения; затем необходимы анализ, evidence gate, новое scientific review и отдельный evidence-backed переход закрытия obligation. Этот переход пока не реализован. Разные actor IDs и source digests не удостоверяют независимость автора, контекста или данных; reanalysis здесь использует ту же выборку.

В текущем срезе follow-up формируется planner, а не модельным агентом, и поддерживает только запрос различающего эксперимента. Для остальных finding kinds нужны отдельные переходы. Evidence-backed closure, повторное научное review на новых данных и подтверждённая независимость пока не реализованы. `Kernel.next_action` оставляет claim в `replan`, пока есть открытые obligations в его связанном review context; `PaperBuilder` повторяет этот gate и не допускает внутренний paper scaffold для исходного или successor claim даже после позднего `approve`. Само создание follow-up не устраняет veto. Полный цикл M4 требует перехода закрытия с новым evidence и реального второго run.

Если новый claim ссылается на протокол дочернего follow-up или его потомка, **все** открытые obligations исходного claim удерживают `replan` без отдельной claim-связи. Положительный review нового claim сам по себе их не закрывает.

Позднейший узкий переход [ADR 0010](0010-evidence-bound-obligation-resolution.md) добавил адресное решение исходного reviewer после завершённого дочернего эксперимента и нового evidence-bound review. Описанное здесь состояние `open` относится к этапу до такого решения; остальные findings и устаревшие resolutions по-прежнему блокируют paper gate.
