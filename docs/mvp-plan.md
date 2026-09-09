# План реализации EpistemeOS

Дата: 7 сентября 2026. Это план разработки и критерии приёмки, а не отчёт о завершённом автономном исследовании. Основания: [архитектура](architecture.md), [аудит afterlife](research/afterlife-audit.md), обзоры [AI Scientist / Co-Scientist](research/ai-scientist-coscientist.md) и [Kosmos / Virtual Lab / Robin](research/kosmos-virtual-lab-robin.md), [протокол оценки](research/evaluation-plan.md). [Карта репозитория](repository-map.md) связывает этапы с модулями.

## Результат и границы MVP

Первый этап задачи — изучить исходный подход, обосновать архитектуру и начать реализацию — представлен исследовательскими документами и локальным прототипом. Следующая цель разработки — **исполняемый полный исследовательский цикл**: competing hypotheses → tournament → выбранный эксперимент → frozen protocol → run → проверенное evidence → независимая репликация и scientific review → новый эксперимент либо ограниченный вывод → manuscript и воспроизводимый evidence bundle.

Полный MVP считается готовым, когда этот цикл работает после перезапуска процесса, учитывает отрицательные результаты и бюджет, действительно меняет эксперимент по review и завершается проверяемым paper draft. Прохождение toy demo, совет `next_action="paper_candidate"` или наличие Markdown-отчёта по отдельности этого не доказывают. «Подготовлен пакет для внешнего review» — достижимый технический статус; принятие TMLR/ICLR определяется независимым внешним review конкретной работы.

Первый полный цикл будет небольшим, вычислительным и локальным. Ограничение размера эксперимента не должно ослаблять preregistration, изоляцию ролей, учёт попыток или traceability. Универсальность проверяется вторым domain pack с другой схемой данных, а не только переименованием синтетического примера.

## Что уже есть

Статусы относятся к исходникам текущего v0.1. Проверки подтверждают перечисленные инженерные сценарии, а не научную результативность.

| Возможность | Текущее состояние | Что ещё требуется |
|---|---|---|
| Persistent state | SQLite WAL, append-only events/receipts, SHA-256 blobs, idempotent command admission, study/correlation/causation metadata, аддитивная receipt migration; typed graph. | Study access boundaries, остальные публичные entity schemas, общий migration/restore, supersession/contradiction, внешний checkpoint. |
| Конкурирующие объяснения | `Kernel.hypothesis` требует prediction/falsifier/scope; протокол ссылается минимум на две гипотезы. | Генерация разнообразного pool, проверка различимости предсказаний, literature support, версионирование explanation sets. |
| Preregistration | Immutable protocol; typed estimand/unit/metrics/sample-size/uncertainty/stopping/multiplicity/splits, mode и exposure snapshot; typed amendments требуют основание. | Сопоставление деклараций с фактическими outputs, расчёт статистики, authenticated data access, сложные sequential designs. |
| Runs и provenance | Контракты start/finish; demo реально запускает два фиксированных Python-приложения, сохраняет raw CSV, metrics, argv, runtime и логи. | Общий runner, восстановление процесса, полное source/environment closure, execution attestation, sandbox и измерение ресурсов. |
| Mechanical gates | Проверка completeness, hashes, seed coverage, finite primary metric, scope и agreement повторного анализа. | Domain recomputation метрики и статистические проверки; schema/units, planned-versus-observed accounting, объяснимые exemptions. |
| Replication / review | Проверяются заявленные actor IDs, разные implementation digests, basis review, self-review и незакрытый отрицательный verdict. | Аутентифицированные назначения, отдельные контексты и права чтения; реальное независимое выполнение и научная оценка. |
| Replanning | `next_action` вычисляет рекомендацию; CLI принимает отдельно подготовленный review JSON. Demo останавливается перед scientific review. | Durable dispatch, obligations, автоматический новый эксперимент и реально независимые agent sessions. |
| Paper | `PaperBuilder` создаёт внутренний Markdown/JSON scaffold из актуально допущенных claims, хранит immutable artifacts и paper event. | Полноценный manuscript, figure/citation/Methods checks, semantic claim anchors и переносимый venue/reproduction bundle. |
| Tournament / tree | `Search`: immutable pool, A/B pairings, ballots, preference ranking; bounded best-first nodes, persisted selection и резерв объявленной стоимости, links к kernel run/claim. | Durable dispatch/leases, аутентификация, фактический многоресурсный ledger и оценка качества выбора на задачах с известным механизмом. |
| Afterlife adapter | Read-only bounded inspection и идемпотентный импорт historical snapshots с сохранением failures/dirty/source и ограничений hash-проверки. | Исполнение через общий runner, domain metrics/gates, переход исторических кандидатов к новым preregistered исследованиям. |

Различные роли в одном доверенном Python-процессе не доказывают независимости. Различный hash двух программ не доказывает независимого авторства. `python -I` изолирует настройки Python, но не запрещает программе читать файлы, сеть или создавать процессы. Внутренний hash chain не обнаруживает полностью согласованную перепись истории владельцем файлов либо удаление её хвоста без внешнего checkpoint.

Search прототип сохраняет решения и незавершённые резервы между открытиями Store, но сам не запускает процесс. `actual_cost` пока сообщает доверенный caller; при зарегистрированном перерасходе новые selections останавливаются. Это не доказательство жёсткого ограничения реальных расходов API/GPU. Ranking допускает неполное расписание, поэтому число comparisons и abstentions нужно читать вместе с priority; одинаковый рейтинг не означает одинаковую доказательную поддержку.

Paper scaffold сохраняет source snapshot и review bases, а build проходит ту же expected-revision границу записи; materialization повторно проверяет eligibility и bytes. Цифры таблицы читаются из зарегистрированной primary metric. Свободный текст claim и registered Methods пока не проверяется семантически против кода; библиотека не устанавливает научную состоятельность, новизну или соответствие площадке.

## Покрытие исходной задачи

| Требование | Артефакт первого этапа | Доказательство для полного MVP |
|---|---|---|
| Изучить afterlife и пять названных подходов | Аудит и три обзорных документа в `docs/research/`, ссылки на версии и первичные источники. | Заимствованные механизмы реализованы с явно указанными границами и проверены по соответствующим этапам. |
| Предложить архитектуру, repository structure и план | `architecture.md`, `repository-map.md`, этот документ. | Разделение ответственности сохраняется в коде и интеграционных сценариях. |
| Hypothesis tournament и competing explanations | Контракты Hypothesis и Search. | M3: реальный proposal/judge pipeline и проверяемый выбор различающего теста. |
| Experiment tree search | Persisted bounded best-first прототип. | M2–M4: бюджет, dispatch, recovery и фактически исполненная новая ветвь. |
| Persistent state / claim–evidence graph | Event links и immutable blobs. | M1: typed projections, versioning, contradictions и dependency invalidation. |
| Runs, provenance, preregistration и gates | Kernel и выполняемый CPU fixture. | M1–M2: scientific design, source/environment closure, sandbox и fail-closed domain checks. |
| Independent Executor / Replication / Reviewer | Role checks и контракт review. | M4: аутентифицированные назначения, изолированные context bundles, отдельная реализация и независимый verdict. |
| Автоматический цикл до paper | `next_action`, CLI review и внутренний paper scaffold. | M4–M5: review вызывает реальный replanning/run, затем получается трассируемый manuscript и reproducible package. |

## Этапы, зависимости и условные оценки

Оценки ниже — ориентиры инженерного труда для одного опытного разработчика при доступных данных и инфраструктуре, а не обещанные календарные сроки. Они не включают ожидание GPU/API, создание новых научных методов или внешний review. После каждого этапа оценка пересматривается по измеренной сложности. Некоторые задачи допускают параллельную работу.

| Этап | Зависимости | Поставляемый результат | Условная оценка |
|---|---|---|---|
| M0: основания и контрактный прототип | Исходный afterlife и сохранённый контекст | Аудит, обзоры, архитектура, план, API kernel, offline fixture и проверки ограничений. | Текущий этап; проверяется по артефактам, без переоценки задним числом. |
| M1: типизированное состояние и научный протокол | M0 | Schemas, migrations, graph projections, idempotent commands, statistical design contract. | 1–2 инженерные недели. |
| M2: достоверное исполнение и ресурсы | M1 | Изолированный worker, provenance closure, leases/outbox, восстановление, ledger ресурсов. | 2–4 инженерные недели. |
| M3: агентный поиск | M1; реальное исполнение требует M2 | Provider interface, literature records, tournament, experiment frontier и durable selection. | 2–3 инженерные недели. |
| M4: независимый review и замкнутый цикл | M2 + M3 | Clean-room modes, review findings/obligations, automatic replan, два domain packs. | 2–4 инженерные недели. |
| M5: paper и воспроизводимый пакет | M4; формат bundle можно проектировать с M1 | Claim-anchored manuscript, проверка figures/citations, frozen export и reproduction recipe. | 1–2 инженерные недели. |
| M6: оценка harness | Инфраструктурные проверки с M1; полный pilot после M5 | Замороженные baseline/абляции, pilot, затем preregistered сравнение и внешний review. | Pilot: 1–2 инженерные недели; основное исследование — после оценки стоимости и мощности. |

Критический путь: `M1 → M2 → (исполнение M3) → M4 → M5`. Рубрики, задачи evaluation и read-only afterlife importer можно готовить параллельно, но их результаты нельзя выдавать за прошедшие gates до подключения к готовым контрактам. MCTS, graph database, web UI и распределённые GPU backends не блокируют первый полный MVP; расширение инфраструктуры не заменяет закрытие научных требований.

## M1 — состояние, причинность событий и научный дизайн

**Инкремент 9 сентября реализован и локально проверен:** command envelope v1 и atomic receipts; study/correlation/causation metadata; additive migration без изменения v1 event hashes; statistical design v1; declarative exposure и typed claim modes; новый связанный exposure/foreign attempt требует fresh review. Прямые legacy API остаются без idempotency; прошлые protocols не получают statistical classification задним числом. [Command API](command-api.md), [design ADR](decisions/0002-statistical-design.md), [проверки](validation.md).

M1 в целом ещё открыт: публичные schemas всех entity types, ResearchQuestion/ExplanationSet revisions, общие supersession/contradiction relations и descendant invalidation, полноценный migration/restore и planned-versus-observed domain validation не реализованы. Ниже сохранены полные критерии этапа.

1. Ввести versioned schemas для ResearchQuestion, HypothesisVersion, ExplanationSet, Protocol, RunAttempt, Artifact, Observation, Claim, EvidenceLink, Review, Decision и PaperBundle. Разделить техническое состояние запуска и научный исход; у каждого claim есть тип, scope, assumptions и limitations.
2. Добавить study/correlation/causation IDs, command idempotency key, expected revision и schema migrations. Проекции объяснений, дерева поиска и claim–evidence DAG строятся из events и могут пересоздаваться; они не становятся отдельной истиной.
3. Формализовать statistical design: experimental unit, estimand, primary/secondary metrics, sample-size/power rationale, splits, exclusions, uncertainty estimator, stopping и multiple-testing family. Для descriptive протокола допустимо обоснованное отсутствие significance test; пустая строка не отключает обязательные поля.
4. Хранить data exposure и exploratory/confirmatory режим. Поправка после увиденных данных создаёт новый protocol с причиной, видимыми данными и policy подтверждения. Все альтернативные analyses остаются в search history.

**Приёмка M1:** один command ID не создаёт два события; повтор с иным payload даёт conflict; два writers не проходят одну expected revision; replay событий после миграции сохраняет IDs и доказательные связи; dangling links/циклы supersession отвергаются; новый contradicting result инвалидирует зависимые attestations, сохраняя старые версии. Нельзя продвинуть exploratory finding в confirmatory claim без зарегистрированного нового дизайна. В CI есть допустимый null-result и инъекция псевдорепликации/незапланированного выбора метрики, которую соответствующая policy блокирует.

SQLite остаётся локальным transactional backend: один writer service и несколько readers, короткие `BEGIN IMMEDIATE` транзакции, retry только после перечитывания состояния. Долгий LLM/API вызов никогда не удерживает SQL write lock. WAL хранится на локальном диске; несколько машин не разделяют SQLite-файл через сетевой filesystem. Потребность в нескольких write-services потребует отдельного backend и повторных concurrency-проверок.

## M2 — runner, provenance, sandbox и бюджет

Runner получает immutable job specification, не прямой доступ к записи scientific state. До dispatch одной транзакцией создаются attempt, budget reservation и outbox entry. Worker использует lease/heartbeat; успешная финализация проверяет ownership, exit status, expected outputs и manifests. Повторная доставка не запускает второй attempt незаметно.

Manifest фиксирует полный source bundle либо clean commit с захваченными разрешёнными dirty/untracked файлами, lock/container digest, data/model/split revisions, seed, argv, resolved config, CPU/GPU/provider fingerprint, timestamps, stdout/stderr, bytes всех outputs и usage. Секреты и credentials не входят в bundle. Domain checker пересчитывает metric из raw data. Replay-cache и fresh execution имеют разные режимы; cache miss не вызывает сеть.

Изоляция: read-only inputs, отдельный write-directory, явные tool/network capabilities, лимиты CPU/RAM/disk/wall time, завершение дерева процессов. Для Windows/Linux публикуется проверенная матрица возможностей; unsupported capability вызывает отказ, а не молчаливую деградацию. Первый backend может использовать Linux-контейнер, запущенный с Windows через поддерживаемую среду. Его доступность определяется preflight.

Ledger хранит `reserved/spent/released/unknown` отдельно для study, experiment и attempt; валюта/токены/GPU-время/wall time не смешиваются. Retry и concurrent in-flight calls расходуют тот же study budget. Пропавший ответ API сохраняет резерв до reconciliation с фактическим состоянием. Planner заранее оставляет квоты на replication и completion, чтобы поиск не мог потратить весь бюджет до проверки результата.

**Приёмка M2:** restart между reserve/dispatch/output/finalize не теряет и не дублирует attempts; два workers не расходуют последний доступный резерв дважды; завершённый attempt immutable; timeout observer не трактуется как смерть worker; worker не читает запрещённый файл и не пишет в inputs/kernel; ограничение wall time прекращает дочерние процессы. Полный bundle восстанавливает чистый offline запуск и пересчитывает зарегистрированную метрику. Изменение raw bytes, незахваченный исходник, неизвестная среда или недостающий output блокирует evidence.

Внешний checkpoint и подписанное runner attestation связывают bundle с отдельной доверенной границей. Подпись удостоверяет источник записи при принятой модели доверия, не истинность научного вывода.

## M3 — гипотезы, tournament и experiment tree

Минимальный provider interface возвращает typed proposals и сохраняет model/version, prompts, tool results и usage. Поведение mocked provider остаётся для CI; реальные actor sessions получают минимально необходимую проекцию состояния. Literature retrieval создаёт Source/LiteratureClaim с DOI/URL/version, локатором подтверждения и статусом проверки. Проверенный DOI не означает проверенную интерпретацию или новизну.

Турнир сохраняет сильного конкурента и null/artifact baseline. Для небольшого pool используется round-robin, counterbalanced A/B, ties/abstain, rubric и rationale каждого ballot. Оценки plausibility, discriminability, feasibility и cost хранятся отдельно; итоговый priority не является вероятностью истинности. Повторяемые пары позволяют измерять позиционный bias и нестабильность judge. Заявление о novelty требует внешнего источника и признания ограничений поиска.

Tree policy начинает с bounded best-first. Каждое действие (`baseline`, `discriminate`, `ablate`, `robustness`, `debug`, `replicate`) связывает parent, protocol, score components, ожидаемую стоимость, decision rationale, runs и outcome. Fixed policy version/seed дают replay выбора. Tree edges показывают происхождение плана; evidence DAG отдельно показывает зависимости данных. Изменение узла не добавляет независимой выборки.

**Приёмка M3:** рейтинг восстанавливается из ballots после restart, порядок/версии кандидатов сохраняются, отсутствие покрытия пары явно видно; одинаковый snapshot/policy выбирает тот же frontier node; stale selection не допускается после нового evidence; ограничения ширины/глубины и резерв replication соблюдаются. Удаление/отбрасывание узла оставляет причину и все results. На fixture с confound следующий выбранный тест различает два объяснения, а не лишь оптимизирует headline metric. Качество этого выбора оценивается oracle или reviewer, отдельно от валидности записи.

## M4 — независимая проверка и автоматическое replanning

Назначения Executor, Replication и Scientific Reviewer аутентифицируются write-service и связаны с конкретным immutable target. Worker не выбирает себе другую роль строкой в запросе. Context bundles и разрешённые artifact IDs журналируются; reviewer не получает авторство/убедительный REPORT до плана и критериев там, где это допускает задача.

| Режим | Что получает проверяющий | Что проверяет |
|---|---|---|
| `exact_rerun` | Тот же source/environment/data bundle. | Воспроизведение конкретного вычисления; не независимое авторство. |
| `seed_replicate` | Тот же метод и новый зарегистрированный randomness schedule. | Устойчивость к случайности в заданном режиме. |
| `independent_reanalysis` | Estimand/protocol, разрешённые raw data, schema и критерий сравнения; без исходной реализации/вывода в стартовом пакете. | Ошибки реализации анализа на тех же наблюдениях. |
| `new_data_replication` | Метод и новая зарегистрированная выборка/среда. | Перенос результата на новые данные в заявленном scope. |

Отдельно записываются независимость реализации, контекста, модели и данных. Ни один режим не объявляется универсальной гарантией остальных. Required mode выбирается protocol policy до результата; недоступность новых данных сужает claim и отмечается в limitations.

Review содержит findings, severity, evidence references, альтернативные объяснения, statistical assessment и обязательные actions. Блокирующее замечание превращается в obligation с проверяемым closure criterion. Approval другого reviewer не закрывает его голосованием. После изменения evidence требуется новый review basis. Диспетчер обрабатывает решение: repair evidence, различающий эксперимент, narrowing claim, refuted branch, request data либо stop inconclusive. Каждое действие исполняется как новый versioned job, сохраняя причины и causation links.

**Приёмка M4:** независимая session не может прочитать запрещённый source/report; reviewer-конtributor не назначается на собственный target; injected sign/unit bug обнаруживается отдельной реализацией; `request_changes` реально создаёт и выполняет новый protocol/run, после которого obligation закрывается evidence и новым review. При исчерпании бюджета сохраняются frontier, открытые вопросы и honest stop status. Restart между verdict и dispatch не теряет replanning job.

Первый domain pack — synthetic known-ground-truth задача. Второй — afterlife: read-only импорт маленького mock run и цепочки S1 prediction → negative finding → ADR; затем новый различающий вычислительный эксперимент. Import сохраняет source SHA, legacy IDs, failed/superseded outcomes, но не присваивает историческому PLAN задним числом seal или APPROVED. Повторный импорт того же source snapshot идемпотентен. Mock/replay/fresh режимы, provider, context treatment, decoding/model revisions и degeneracy cautions остаются доменными полями. Общий kernel проходит оба pack без проверок по имени afterlife.

## M5 — manuscript из проверенного состояния

Paper builder получает frozen eligibility snapshot. Эмпирические числа и figure data поступают из зарегистрированных observations, а не из свободного текста writer. Для каждого empirical anchor существует путь `paper → claim/observation → artifact → run → protocol → code/environment/data`; figure хранит использованный data table и рецепт построения. Literature assertions имеют Source/LiteratureClaim и проверенный locator. Exploratory discussion помечается отдельно.

Builder создаёт manuscript, methods/provenance appendix, bibliography, limitations, deviations и negative-results inventory, code/data availability, AI contribution disclosure, reproduction recipe и venue-policy snapshot. Methods проверяется на соответствие исполненному коду, а scope Conclusions — актуальным claims. Изменение claim/evidence/review делает старую сборку исторической и требует нового eligibility check непосредственно перед финализацией.

**Приёмка M5:** воспроизводимая offline сборка по одному snapshot; каждый empirical anchor разрешается до проверенных bytes; invented number, missing citation locator, missing figure data, stale approval или открытый blocking finding предотвращает готовый release bundle. Reviewable Markdown/LaTeX и rendered PDF визуально проверяются. Человек может пересчитать ключевую таблицу по export в чистой среде. Автоматическая загрузка manuscript на площадку не входит в MVP; human release и внешнее peer review остаются явными событиями.

## M6 — доказательства результативности

Выполнить [план оценки](research/evaluation-plan.md) поэтапно: fault injection → development pilot → freeze → подтверждающий benchmark → независимый human review. Baseline L сохраняет ту же дисциплину provenance/gates; F и четыре абляции сравниваются при равном потолке ресурсов, включая debate/review/replication. Development, pilot и holdout разделены, oracle недоступен harness.

Основные outcomes: доля корректно решённых задач и частота необоснованных headline claims. Показывать мощность/неопределённость, все ошибки/отказы/timeouts и стоимость. Учитывать семейства задач, псевдорепликацию и multiple comparisons; не считать каждый branch отдельной независимой научной задачей. Размер подтверждающего запуска и допустимые границы риска фиксируются до holdout по pilot, а не выбираются по выгодному результату.

**Приёмка M6:** опубликованы замороженные варианты, назначенные попытки, все outcomes/deviations, preregistered analysis и независимые оценки. Это означает, что исследование harness выполнено корректно; преимущество F не является условием честного завершения оценки. Отрицательный или неопределённый результат меняет следующий план и ограничения заявлений. Утверждать научную эффективность можно только в проверенном scope с соответствующей точностью.

## Сквозной сценарий завершения полного MVP

1. Создать study с ограниченным бюджетом, минимум двумя конкурирующими объяснениями, зарегистрировать tournament и выбрать различающий experiment node.
2. Заморозить protocol; worker выполняет реальные вычисления и сохраняет provenance, raw/derived artifacts и все attempts.
3. Независимый analysis/replication проверяет ключевую метрику; gate принимает либо блокирует evidence с конкретным основанием.
4. Scientific Reviewer обнаруживает заранее заложенный confound и возвращает обязательный control. Dispatcher создаёт новый protocol, выполняет control, сохраняет исходный отрицательный результат и пересматривает claim.
5. После актуального независимого review writer собирает manuscript с ограниченным выводом и полностью трассируемыми таблицами/figures; отдельный сценарий завершает исследование как inconclusive по бюджету.
6. Перезапустить manager/worker в контролируемых точках цикла; результат не дублируется, reservations сходятся, историю можно воспроизвести. Повторить переносимую часть на afterlife pack.
7. Перед объявлением готовности сопоставить каждый критерий M1–M5 с именованным тестом либо проверенным run/bundle. Непроверенный пункт остаётся открытым, даже если остальные проверки зелёные.
