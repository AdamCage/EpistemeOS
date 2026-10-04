# Карта репозитория EpistemeOS

Дата: 4 октября 2026. Здесь отдельно описаны существующие файлы v0.1 и проектируемые модули. Архитектурные решения — в [architecture.md](architecture.md), зависимости и приёмка — в [mvp-plan.md](mvp-plan.md). Названия будущих каталогов задают границы ответственности; пустые пакеты ради этой схемы создавать не требуется.

## Фактическое ядро v0.1

```text
EpistemeOS/
├── README.md                     статус и локальные команды
├── AGENTS.md                     контракт участников разработки
├── objective.md                  сохранённый исходный контекст
├── pyproject.toml                Python package и entry point episteme
├── uv.lock                       workspace manifest; runtime dependencies отсутствуют
├── docs/
│   ├── architecture.md           система состояний, роли и границы доверия
│   ├── mvp-plan.md               этапы, зависимости, критерии приёмки
│   ├── repository-map.md         эта карта
│   ├── command-api.md            versioned local command interface
│   ├── recovery.md               directory snapshot и восстановление receipts/CAS
│   ├── adversarial-audit-2026-10-04.md  состязательный аудит da6aa2a и статус исправлений
│   ├── decisions/                command, workflow и agent proposal ADRs
│   └── research/
│       ├── afterlife-audit.md
│       ├── ai-scientist-coscientist.md
│       ├── kosmos-virtual-lab-robin.md
│       └── evaluation-plan.md
├── src/episteme/
│   ├── __init__.py
│   ├── __main__.py               python -m episteme
│   ├── cli.py                    demo / inspect / agent / execution / batch / analysis / graph / command / backup / restore
│   ├── store.py                  SQLite events/receipts и content-addressed blobs
│   ├── recovery.py               согласованный backup и verified restore в новый каталог
│   ├── commands.py               versioned allowlist, type/role admission, normalization
│   ├── protocols.py              immutable statistical declarations и validation
│   ├── agents.py                 proposal tasks; schema 3 идёт через pack, schema 2 — через legacy compiler
│   ├── agent_controller.py       one-shot dispatch/reconcile/advance вне SQL
│   ├── agent_profiles.py         append-only hypothesis/experiment profile registry
│   ├── agent_proposals.py        frozen v1 prompt, schemas и semantic validator
│   ├── experiment_proposals.py   замороженный experiment-proposal-v1 (synthetic recipe)
│   ├── experiment_proposals_v2.py  конверт experiment-proposal-v2 без доменных параметров
│   ├── pack_proposals.py         закрепление схемы пакета и контекст модели без host inputs
│   ├── legacy_experiment.py      замороженный compiler для agent_request schema 2
│   ├── codex_provider.py         fingerprinted CLI adapter и frozen wrapper
│   ├── planning.py               версии ResearchQuestion/ExplanationSet, bindings и ancestry
│   ├── execution.py              atomic job admission, one-shot dispatch, verified reconciliation
│   ├── batch.py                  frozen primary/reanalysis roster, attempt ownership и settlement
│   ├── batch_controller.py       последовательный resume по сохранённым slots
│   ├── batch_analysis.py         claim + analysis provenance одной receipt
│   ├── analysis_controller.py    resume analysis → review assignment
│   ├── proposal_execution.py     applied proposal schema 2 или pack schema 3 → selected frozen batch в одной receipt
│   ├── replanning.py             negative review → typed открытые obligations
│   ├── resolution.py             reviewer opinion → evidence-bound obligation resolution
│   ├── review_admission.py       семейства claims и общая проекция veto/obligations/resolutions (ADR 0018)
│   ├── review_assignment.py      frozen контекст назначения без identity/read isolation
│   ├── reviewer_controller.py    durable projected выдача, raw ответ и unknown/reconcile
│   ├── review_submission.py      verdict из delivered response → review/obligations/provenance
│   ├── followup.py               obligation → frozen дочерний protocol/node/binding
│   ├── followup_execution.py     winning follow-up node → selected frozen batch
│   ├── execution_authority.py    локальный marker вне backup/CAS, запрет запуска restored batch
│   ├── runner_backend.py         trusted local Python process supervisor, bounded logs и completion
│   ├── environment_closure.py    профиль v2: манифест дерева, правила uv.lock, closure и allowlist переменных
│   ├── execution_locked.py       профиль v2: freeze, specification, каталог job вне хранилища, completion, CLI
│   ├── runner_locked.py          профиль v2: offline uv-окружение, запись установленного, gated payload
│   ├── reproduction.py           episteme reproduce: записанный повтор того же кода, никогда не evidence
│   ├── claims.py                 типизированный immutable ClaimLink
│   ├── claim_context.py          scope/циклы связей и транзитивный review context
│   ├── kernel.py                 валидируемые научные команды и gates
│   ├── domain_binding.py         receipt-backed manual frozen domain recipe
│   ├── domain_packs.py           pack.preregister/pack.analyse, replay, CasView allowlist и потолок силы claim
│   ├── search.py                 persistent tournament и bounded tree policy
│   ├── reporting.py              snapshot export и внутренний paper scaffold; колонка roster по roster_semantics
│   ├── graph.py                  типизированная read-only проекция и queries
│   ├── domains/afterlife.py      bounded historical inspection/import
│   ├── domains/afterlife_seed.py verified historical steps bundle и два offline runner sources
│   ├── domains/afterlife_seed_batch_analysis.py  предметный пересчёт nine-trajectory batch
│   ├── domains/synthetic_causal.py  synthetic fixture recipe и runner sources
│   ├── domains/synthetic_batch_analysis.py  пересчёт synthetic metric из raw data
│   ├── domains/api.py            DomainPack envelopes, StatisticalReport v1 и hook-facing CasView (ADR 0016)
│   ├── domains/registry.py       явный allowlist пакетов, code manifest и загрузка закреплённых bytes
│   ├── domains/packs/synthetic_causal_v1/  фасад synthetic pack: те же программы, recipe и estimator
│   ├── domains/packs/afterlife_seed_v1/    фасад afterlife pack: read-only capture, чистая проверка inventory, recount
│   ├── domains/packs/tabular_classification_v1/  confirmatory holdout comparison на сгенерированной таблице, профиль v1
│   └── demo.py                   два фиксированных CPU-приложения
├── schemas/                      command, statistical design, question/set, claim link, review, experiment proposal, девять DomainPack envelopes и pack code manifest
├── examples/model_hypotheses.py  подготовка одного задания; --execute явно вызывает модель
├── examples/model_experiment.py  frozen synthetic experiment request; --execute явно вызывает модель
├── examples/afterlife_historical_pilot.py  подготовка exploratory batch по проверенному legacy run
├── examples/tabular_classification_v1/  генератор и две сгенерированные CSV-таблицы mechanism test, не научный набор данных
└── tests/
    ├── test_kernel.py            инварианты ядра и исторические failure cases
    ├── test_cli.py               реальные CLI/subprocess интеграции
    ├── test_search.py            ballots, tree bounds, reservations и replay
    ├── test_reporting.py         snapshots, review input и paper eligibility
    ├── test_graph.py             typed refs, graph traversal и corruption
    ├── test_afterlife.py         historical import, limits и idempotency
    ├── test_commands_store.py    receipt integrity, atomicity, competing writers и crash
    ├── test_store_verification.py проверенный снимок: подмена, усечение, откат, конкуренция и счётчики работы
    ├── test_commands_service.py  versioned dispatch, historical replay и real CLI
    ├── test_protocols.py         typed statistical declarations
    ├── test_planning.py          immutable revisions, exact refs, scope, exclusions и head races
    ├── test_planning_workflow.py protocol/review/study/graph binding, CLI и recovery
    ├── test_execution.py         реальные jobs, controller crash, concurrency, Graph/CAS/backup
    ├── test_batch.py             полный roster, failures/unknown, atomicity, restore guard и CLI
    ├── test_batch_analysis.py    полный batch → claim → assignment, replay, CLI и restore
    ├── test_domain_binding.py    manual recipe → batch → analysis, drift, CLI и restore
    ├── test_afterlife_seed_batch.py  исторический inventory, схема записей шагов, offline batch/analysis, drift и подмена
    ├── test_synthetic_batch_analysis.py  пересчёт raw metrics, frozen parameters и oracle boundary
    ├── test_execution_authority.py atomic marker, concurrency, corruption и restore
    ├── test_runner_backend.py    реальные descendants, timeout, capture cap и duplicate delivery
    ├── test_execution_locked.py  профиль v2: форматы, offline uv, allowlist, каталог вне хранилища, CLI
    ├── test_reproduction.py      matched/mismatched, basis, unknown, v1 при том же интерпретаторе, CLI
    ├── test_locked_pack.py       профиль v2 в pack.preregister и batch; fixture вне реестра; replay не evidence
    ├── test_claims.py            shape и hashes immutable claim links
    ├── test_claim_context.py     scope, direction, cycles и context closure
    ├── test_claim_workflow.py    review propagation, veto, supersession, paper и CLI replay
    ├── test_claim_lineage.py     последовательные версии и current replacement frontier
    ├── test_recovery.py          exact state/receipt replay, corruption и recovery CLI
    ├── test_recovery_concurrency.py snapshot при append, no-overwrite race, manifest/binding corruption
    ├── test_scientific_workflow.py exposure timing, mode, amendments и review invalidation
    ├── test_cli_recipe.py        host binding через реальный CLI без событий/model call
    ├── test_experiment_agents.py atomic proposal application, stale/replay/recovery
    ├── test_proposal_execution.py atomic selection/batch, replay и rollback
    ├── test_replanning.py       review/obligation basis, replay и paper veto
    ├── test_resolution.py       exact review/resolution receipt, stale basis и sibling veto
    ├── test_review_assignment.py context policy, conflict, replay, Graph и restore
    ├── test_reviewer_controller.py доставка, submission, unknown, replay и CLI
    ├── test_reporting_followup.py paper lineage и stale/sibling veto
    ├── test_review_families.py  семейства claims: повторный claim, перерегистрация на тех же bytes, scope resolution
    ├── test_followup.py         дочерний protocol/node, stale source, budget и no closure
    ├── test_followup_execution.py atomic selection/batch, stale basis, receipt replay и restore
    ├── test_experiment_graph.py  typed refs, schema export и tamper rejection
    ├── test_experiment_proposals.py strict schema и frozen hypothesis order
    ├── test_synthetic_causal.py  fixture recipe, estimates и real runner
    ├── test_domain_api.py        строгие envelopes, StatisticalReport v1 и опубликованные schemas
    ├── test_golden_history.py    basis/gates/Graph/export сохранённых histories не меняются
    ├── golden_support.py         загрузка и наблюдение сохранённых fixture histories
    ├── fixtures/golden/          три synthetic histories от кода 483f1bd и их ожидаемые hashes
    ├── test_domain_pack_conformance.py  общий conformance suite пакетов, pinning, drift и import contract
    ├── test_synthetic_pack.py    synthetic pack против legacy compiler/adapter на golden histories
    ├── test_pack_workflow.py     pack.preregister → batch → pack.analyse → assignment, tamper suite, restart и restore
    ├── test_pack_universality.py ядро без импортов пакетов и pack ID; command schema равна runtime
    ├── test_pack_lineage.py      привязка пакета управляет линией protocol: amendment, follow-up, закреплённые bytes, stray claim
    ├── test_pack_cli.py          CLI по привязке, pack describe и read-only pack verify
    ├── test_afterlife_pack.py    capture/compile против legacy, отказы захвата и CLI путь capture → анализ
    ├── test_tabular_pack.py     confirmatory потолок, inconclusive при невыполненном правиле, exposure tripwire
    ├── pack_fixtures.py          conformance-входы пакетов и минимальный локальный runner без Store
    ├── review_paths.py           тестовый путь review: assign → fixture provider → review.submit (ADR 0018)
    ├── fixtures/packs/           conformance_fixture_v1 и locked_fixture_v2; оба только для проверки контракта
    └── test_workflow.py          фактический search → execution → evidence demo
```

`search.py` — самостоятельный модуль над существующим Store/Kernel. Его наличие не превращает CLI в автономный scheduler. README, packaging и CI описывают запуск и проверенные платформы; они не являются scientific evidence.

| Файл | Реальная ответственность | Граница |
|---|---|---|
| `agents.py`, `agent_controller.py`, `codex_provider.py` | Frozen hypothesis/experiment requests, bounded local CLI, response retention, atomic hypotheses/set или protocol/tree node application. Schema 3 замораживает pack protocol и binding; schema 2 остаётся на legacy compiler. | Caller-declared actors и trusted OS/account; нет authenticated independence, model-science approval или автоматического запуска предложенного эксперимента. |
| `experiment_proposals.py` | Замороженный prompt, provider schema и validator `experiment-proposal-v1`. Имя recipe в тексте — часть исторического контракта. | Валидный JSON не доказывает различающую силу эксперимента или реальное исполнение. |
| `experiment_proposals_v2.py`, `pack_proposals.py` | Конверт `experiment-proposal-v2` и закрепление схемы параметров пакета до вызова модели. Host inputs в контекст модели не входят. Применение пишет protocol, `pack_binding` и узел. | Пакет не повышает силу claim, не пишет review и не удостоверяет независимость. `scientific_validity` остаётся `not_assessed`. |
| `legacy_experiment.py` | Замороженный compiler для `agent_request` schema 2. Replay уже записанного application его не вызывает; новая запись schema 2 по-прежнему компилирует им. | Это не обобщённый путь и не доказательство, что второй домен проходит schema 2. |
| `domains/synthetic_causal.py` | Синтетический causal recipe, отдельные source для primary/reanalysis и typed exploratory design. | Известный генератор и общие наблюдения для повторного анализа; нет внешнего scientific evidence или доказанной независимости второго анализа. |
| `batch.py`, `batch_controller.py` | Полный roster primary/reanalysis для выбранного scientific node, резерв будущих slots, resume без повторного dispatch, полный технический settlement. Recipe каждой пары проверяется по профилю её environment (v1, v2 или смесь). | Fresh primary policy; стоимость в attempts, без agent reasoning или научной оценки. Capabilities должны поддерживаться профилем каждой пары. |
| `batch_analysis.py`, `analysis_controller.py`, `domains/synthetic_batch_analysis.py` | На frozen synthetic recipe пересчитывают observed point estimates, фиксируют proposal/code CAS, полный claim и отдельно reviewer assignment с resume после сбоя. `analysis.apply` загружает адаптер из реестра `LEGACY_ANALYSIS_ADAPTERS` и допускает только proposal, равный пересчёту на том же снимке (`batch_analysis` schema 2, ADR 0018). | Тот же OS actor может прочитать world; source digest не attestation. Нет verdict или универсального анализа; второй, исторический адаптер описан ниже. |
| `domains/afterlife_seed.py`, `domains/afterlife_seed_batch_analysis.py`, `examples/afterlife_historical_pilot.py` | До CAS проверяют 30 заявленных outputs одного S1 run, hashes, seed grid, схему каждой записи шага и legacy counters; готовят exploratory protocol, `domain.bind` и batch из 9 primary и 9 same-data reanalysis slots; адаптер повторно сверяет bundle, raw bytes и метрики и предлагает только `inconclusive` claim. | Уже наблюдённые данные одного model/configuration; согласие с manifest — предусловие захвата, поэтому конкурирующее объяснение не получает исхода. Нет embeddings для S1 semantic gap, независимого авторства программ, sandbox или verdict. |
| `proposal_execution.py` | Одной planner receipt связывает текущий winning applied model experiment node с selection и полным frozen batch из первоначальной compilation. | Не выбирает узел вопреки priority, не запускает worker и не оценивает научную состоятельность дизайна. |
| `replanning.py`, `resolution.py` | Отрицательное мнение reviewer и typed obligations; затем адресное удовлетворение одного `discriminating_experiment` finding новым reviewed claim на неизменённом evidence basis. Historical receipt и текущий effective status проверяются отдельно. | Роль/ID заявлены caller; решение reviewer не доказывает научную истину или независимость, остальные findings остаются открытыми. |
| `review_admission.py` | Линия protocols (amendment, follow-up, общие наблюдённые bytes, `supersedes`) и семейство claim по событиям снимка; проекция veto, obligations, resolutions и допуска approvals для `next_action`, paper и guard связей; `attempt_ledger` — реестр всех попыток семейства и связанных регистраций с `family_ledger_digest` для manifest v2, bundle v2 и paper; кеш только в read scope или транзакции команды. | Решение механическое: семейство по общим bytes может блокировать и независимые вопросы; actor IDs caller-declared, научная правильность мнений не оценивается. |
| `review_assignment.py` | На текущем mechanically passed basis сохраняет одну receipt, reviewer ID и curated CAS manifest предполагаемого initial context. Historical replay пересчитывает bytes и проверяет contributor conflict. | `caller_declared` identity и `not_enforced` read isolation; legacy review commands не требуют назначения. Manifest не закрывает доступ к Store или утечку смысла через свободный текст. |
| `reviewer_controller.py`, `review_submission.py` | Durable projected request до внешнего вызова, raw response/status, unknown без повтора; затем reviewer opinion/typed obligations, привязанные к assignment и текущему evidence basis одной receipt. | Provider выполняется в доверенном локальном процессе, может читать Store; `review.finalize` и actor ID не аутентифицированы. Synthetic response не является научной экспертизой. |
| `followup.py` | Один открытый запрос различающего эксперимента → frozen дочерний protocol/node/binding с проверкой текущего source basis и бюджета. | Planner-authored план, не запуск, научное подтверждение, независимое review или закрытие obligation. |
| `followup_execution.py` | Повторно проверяет source review/basis и planning, связывает текущий winning follow-up node с frozen batch и exact receipt. | Recipe подаёт planner; selection и резерв не запускают worker и не закрывают научное замечание. |
| `execution_authority.py` | Локальный token и проверка hash перед изменением execution state batch; DB/CAS restore не получает token. | Не аутентификация и не distributed lease; полное копирование marker владельцем файлов может создать исполняемый clone. |
| `store.py` | Canonical JSON, CAS, verified chain/receipts с инкрементальной перепроверкой по отпечатку базы и read scope для CAS (ADR 0017), atomic command transaction/replay, additive receipt migration и export. | Нет аутентификации, внешнего checkpoint, общего schema migration или distributed storage; производные индексы пересчитываются в каждом вызове. |
| `recovery.py` | SQLite online backup, полный наблюдаемый CAS, manifest, semantic closure и restore с точной историей/receipts; эксклюзивный новый destination. | Не переносит процессы/внешнюю среду; filesystem доверенный, нет внешней аутентификации или атомарной видимости всего каталога. |
| `commands.py` | Явный action/role allowlist, strict JSON, аргументы и defaults v1, допуск до handler, historical acknowledgement. | Доверенный local caller; нет внешнего execution или меж-study access boundary. |
| `protocols.py` | Frozen design dataclasses, units/estimand/metrics/splits, mode и structural statistical validation. | Не проверяет actual data, мощность, реальную независимость или uncertainty computation. |
| `planning.py` | ResearchQuestion/ExplanationSet v1, immutable parent refs, heads, hashes/scope, exact exclusion reasons; frozen context с прежними гипотезами. | Constraints и comparison plan декларативны; нет генерации/научной оценки, resource enforcement или actor authentication. |
| `execution.py` | Atomic run/job и result/finalized, unique dispatch, historical receipts, completion identities/hashes, context и CAS closure. | Нет auto-reclaim/retry, signed attestation, study resource ledger или cross-clone exactly-once. |
| `runner_backend.py` | Frozen single Python source/input, отдельный cwd, gated process launch, Windows Job Object / POSIX group, bounded capture, durable completion. | Trusted local profile, без filesystem/network sandbox, package environment reconstruction или domain metric recomputation. |
| `environment_closure.py`, `execution_locked.py`, `runner_locked.py` | Профиль v2 [ADR 0019](decisions/0019-execution-profile-v2.md): многофайловое дерево и uv-closure в CAS, новое offline-окружение на job, allowlist переменных, каталог job вне корня хранилища, запись дистрибутивов, интерпретатора, inventory и GPU. | Тот же OS user, без filesystem/network sandbox; программа достигает хранилища по абсолютному пути. Сеть разрешена только явной `execution prepare --online`, без событий. |
| `reproduction.py` | `episteme reproduce`: durable intent, повтор из CAS в новом каталоге, детерминированное сравнение outputs и окружения, replay-проверка, попытки в basis claim. | Тот же код и данные: не independent replication, не run и не evidence; совпадение outputs не доказывает правильность вычисления. |
| `claims.py`, `claim_context.py` | Immutable proposals отношений, validation порядка/scope/циклов; review context из incoming supports/limits и symmetric contradictions/supersession. | Не доказывают научную связь; binding evidence basis и bytes проверяет Kernel/Graph. |
| `graph.py` | Immutable typed nodes/edges, reference closure, ancestor/descendant queries, exact scope filter, JSON/DOT. | Проекция текущих event types, не scientific adjudication или inferred causal graph. |
| `domains/afterlife.py` | Bounded read-only scan, frozen metadata/blob snapshot, сохранение legacy status/dirty/superseded, idempotent import. | Исторические данные не становятся accepted claims; импорт сам не создаёт runs. Пересчёт stop-событий одного run выполняет отдельный пилот выше. |
| `domains/api.py` | Frozen envelopes ADR 0016 (`PackManifest`, `ParameterCatalog`, `ProtocolDraft`, `ExecutionPlan`, `ExecutionPlanV2`, `SourceTree`, `CaptureBundle`, `OutputCheck`, `Recomputation`, `AnalysisReport` v2, `StatisticalReport` v1) и runtime-проверка по тем же schemas, что опубликованы в `schemas/`. | Проверяет форму, согласованность полей и допустимость `not_applicable` для preregistered design, но не правильность статистики. План v2 не делает вычисление научно верным. |
| `domains/registry.py` | Явный allowlist `pack_id → package`; хеширует все файлы каталога пакета, исполняет именно эти bytes под приватным именем модуля, проверяет manifest, hooks и статический import contract. Отдельный явный allowlist `LEGACY_ANALYSIS_ADAPTERS` legacy-адаптеров анализа для пересчёта в `analysis.apply` (ADR 0018). | Пакет остаётся доверенным Python в процессе ядра: digest выявляет drift и чужую версию, но не вредоносный код, подмену интерпретатора или чтение вне контракта. |
| `domains/packs/afterlife_seed_v1/` | `capture.py` единственным в пакете читает файлы исторического run; `inventory.verify` по bytes повторяет проверки legacy захвата и собирает тот же `input.dat`; программы и recount совпадают с legacy; `validate_protocol` требует объявленной экспозиции. | Уже наблюдённые данные одного model/configuration; report оставляет `inconclusive`/`exploratory`, interval и effect size не вычисляются. Legacy `afterlife_seed*.py` по-прежнему обслуживают ADR 0015 binding. |
| `domains/packs/synthetic_causal_v1/` | Manifest, catalog, compile и analysis hooks synthetic fixture; программы, recipe и estimator перенесены из legacy-модулей без изменения вычислений, world объявлен скрытым входом. Пакет также объявляет схему параметров для `experiment-proposal-v2`. | Fixture с известным генератором; report оставляет `inconclusive`/`exploratory`. Legacy `synthetic_causal.py` обслуживает schema 2 и `domain.bind`. |
| `domains/packs/tabular_classification_v1/` | Confirmatory сравнение logistic regression и majority baseline на отдельных training и holdout CSV; полный `StatisticalReport`; профиль v1. | Сгенерированная таблица, не выборка из популяции. Потолок может быть confirmatory; `scientific_validity` остаётся `not_assessed`. Hooks не получают Store. |
| `tests/golden_support.py`, `tests/fixtures/golden/` | Три synthetic fixture histories, созданные немодифицированным кодом `483f1bd`, и ожидаемые basis, gates, Graph, export bundle и replay-проекции. | Сравнение механическое: совпадение не означает научной валидности; новые event kinds в этих histories не представлены. |
| `tests/fixtures/adr0018/`, `tests/test_historical_admission.py` | Три synthetic histories, записанные кодом аудита `da6aa2a` скриптом `generate.py` на снимке `git archive`: legacy approval и paper demo, approval по подложному анализу schema 1, approval неканонического reviewer. | Approvals в них — fixtures, принятые старым кодом; тесты проверяют, что текущие правила их не засчитывают. |
| `domain_packs.py` | `pack.preregister`: одна receipt `[protocol, pack_binding]` с pin кода, envelopes и plan v1 или v2; для v2 closure хоста сверяется с файлами проекта и требуемыми переменными плана. `pack.analyse`: сверка pin, envelopes, вычисляемых ядром полей, потолка силы claim и повторное исполнение hooks в команде, receipt `[claim, pack_analysis]`; структурный replay без импорта кода пакета; allowlist `CasView`. Review exclusion привязки v2 включает файлы деревьев и проекта. | Пакет — доверенный код в процессе ядра. Потолок механический, `scientific_validity=not_assessed`; повторное исполнение подтверждает воспроизводимость на snapshot, а не правильность статистики. |
| `domain_binding.py` | Ручная receipt-backed привязка planning-bound protocol к CAS recipe/adapter source и точным параметрам batch до исполнения; replay и Graph проверяют исходный префикс. | Доменный смысл recipe проверяет адаптер; actor ID и code digest не доказывают независимость или исполнение именно этих bytes в изолированной среде. |
| `kernel.py` | Hypothesis/protocol/run/result/claim/review commands, правила ролей, binding digests/scope, seeds и run limit, review basis и `next_action`; открытые typed obligations удерживают `replan` и paper gate. | Python caller доверенный. Проверка finite metric не пересчитывает науку. `next_action` возвращает решение, не job. |
| `search.py` | `register_tournament`, `pairings`, `ballot`, `ranking`; `register_tree`, `add_node`, `select_next`, `tree_state`, `finish_selection`. Сохраняются policy, ballots, nodes, решения и резервы объявленной стоимости. | Нет LLM judge, worker dispatch/lease или независимого измерения расходов. Priority не продвигает claim и допускает неполное сравнение pool. |
| `demo.py` | Генерация синтетических CSV и два способа OLS в реальных subprocess, локальные actors; завершение перед review. | Только фиксированные программы, без LLM и независимого scientific review. Runtime record не восстанавливает произвольную среду. |
| `reporting.py` | Один snapshot для inspect/export; `PaperBuilder.build` проверяет текущую eligibility и записывает immutable Markdown/JSON+paper event с трассировкой привязанного review-driven follow-up; `materialize` повторно проверяет basis и bytes. | Внутренний scaffold, без полноценного literature/figures/Methods validation или venue formatting; свободный claim text не сертифицируется. |
| `cli.py` | Demo/inspection/gates/export, review JSON, paper scaffold; `agent recipe --input` замораживает host-owned binding, `agent advance` продолжает оба proposal tasks; `analysis status/advance` берёт пакет или legacy adapter из привязки protocol (`--adapter` лишь утверждение); read-only `pack describe`/`pack verify` показывают живой код пакета и повторно исполняют закреплённые hooks. Остальные переходы принимаются через `episteme command`. | Actor ID задаётся доверенным caller; sandbox и независимого scientific review нет. |
| `tests/test_kernel.py` | Протокол до run, источники evidence, scope, budgets при конфликте writers, retention failures, stale review, self-review, integrity. | Unit tests не доказывают clean-room, sandbox, научную правильность или публикационное качество. |
| `tests/test_search.py`, `tests/test_reporting.py`, `tests/test_cli.py` | Поиск и cost reservations, snapshot consistency и paper eligibility, входные review JSON и запускаемые CLI/subprocess сценарии. | Покрытие конкретных failure cases не означает общего доказательства безопасности либо работы независимых научных агентов. |
| `docs/research/*` | Проверяемые основания решений и заранее предлагаемый evaluation design. | Литературный обзор и локальный code audit не означают независимого запуска внешних систем. |
| `docs/adversarial-audit-2026-10-04.md` | Воспроизведённые находки состязательного аудита `da6aa2a` с PoC; раздел «Статус исправлений» отслеживает исправления по [ADR 0018](decisions/0018-claim-families-and-review-admission.md). | Аудит локального снимка, не научная оценка и не внешний security review; код после `da6aa2a` проверяется только по мере исправлений. |

До выделения новых модулей следует сохранять работающие команды и понятные импорты. Усложнение структуры допускается вместе с реальным переносом ответственности и необходимой проверкой совместимости.

## Предлагаемая структура полного MVP

Все каталоги ниже, кроме явно существующих файлов, **планируются**. Не подразумевается, что перечисленные API уже доступны.

```text
EpistemeOS/
├── src/episteme/
│   ├── cli.py
│   ├── kernel.py                 публичная командная граница
│   ├── schemas/                  versioned records и command validation
│   ├── state/
│   │   ├── events.py             event envelope, causation, idempotency
│   │   ├── sqlite.py             транзакции и migrations
│   │   ├── artifacts.py          blob closure и immutable manifests
│   │   ├── projections.py        explanation/tree/claim views
│   │   └── queries.py            scope-aware evidence/context queries
│   ├── protocols/
│   │   ├── preregistration.py    seal, amendment, data exposure
│   │   └── statistics.py         estimands, units, splits, multiplicity
│   ├── search/
│   │   ├── hypotheses.py         pool, alternatives, provenance
│   │   ├── tournament.py         ballots и воспроизводимый ranking
│   │   ├── tree.py               nodes, edges, frontier, bounded policy
│   │   └── replanning.py         decisions → obligations → proposals
│   ├── orchestration/
│   │   ├── service.py            единственный writer и admission API
│   │   ├── assignments.py        identity, role, target, capabilities
│   │   ├── dispatcher.py         outbox, leases, heartbeat, reconciliation
│   │   └── budgets.py            reserved/spent/released/unknown ledger
│   ├── runners/
│   │   ├── base.py               backend capabilities и job contract
│   │   ├── local.py              поддерживаемый локальный backend
│   │   ├── sandbox.py            inputs, egress, resources, process tree
│   │   ├── provenance.py         source/environment/data/usage closure
│   │   └── replay.py             sealed cache, explicit replay mode
│   ├── agents/
│   │   ├── provider.py           provider-neutral structured invocation
│   │   ├── contexts.py           role-specific context bundles
│   │   ├── planner.py            hypotheses и experiment proposals
│   │   ├── executor.py           code proposals и job requests
│   │   ├── analyst.py            observations и draft claims
│   │   ├── replication.py        независимые реализации по policy
│   │   └── reviewer.py           plan-first findings, scientific verdict
│   ├── literature/               sources, locators, retrieval, checks
│   ├── gates/                    required checks и typed exemptions
│   ├── review/                   immutable bundles, findings, decisions
│   ├── paper/                    anchors, tables, figures, bibliography
│   └── domains/
│       ├── base.py               DomainPack interface
│       ├── synthetic/            known-ground-truth development case
│       └── afterlife/            importer, runner, metric/domain checks
├── schemas/                      публичные JSON schemas по версиям
├── policies/                     versioned search/review/budget profiles
├── prompts/                      role prompts с идентичностью версии
├── examples/                     маленькие входные study specs
├── tests/
│   ├── unit/                     domain-free contract checks
│   ├── integration/              restart, concurrency, sandbox, CLI
│   ├── failure_injection/        corruption, duplicate delivery, leakage
│   └── fixtures/                 явно синтетические безсекретные inputs
├── evaluation/                   baseline/абляции, scoring, анализ
├── docs/
│   ├── research/                 основания и frozen evaluation design
│   ├── decisions/                ADR при изменении архитектуры
│   └── operations/               recovery и воспроизводимый запуск
└── .github/workflows/            offline verification и capability matrix
```

Каталог `src/episteme/search/` заменит плоский прототип `search.py`, когда появятся независимо развивающиеся tournament/tree/replanning компоненты. Аналогично `Store` можно сначала оставить фасадом над выделенными state-модулями. Это не требование немедленного рефакторинга.

## Контракты между модулями

| Производитель → потребитель | Передаваемый объект | Кто допускает переход |
|---|---|---|
| Literature / Planner → Kernel | Source/LiteratureClaim, HypothesisVersion, ExperimentSpec. | Kernel валидирует схему/links; scientific suitability оценивается отдельно. |
| Search → Dispatcher | SelectedNode с policy version, state revision, budget estimate и rationale. | Writer service атомарно проверяет revision, quota и создаёт reservation/job. |
| Dispatcher → Runner | Frozen JobSpec, attempt/lease, source/environment/input digests, capability profile. | Runner preflight; несовместимые capabilities блокируют запуск. |
| Runner → Evidence pipeline | ExecutionRecord, immutable outputs, timing/usage, terminal outcome. | Service проверяет assignment/attempt; gates проверяют closure и DomainPack recomputation. |
| Analyst → Review | DraftClaim с scope, uncertainty, dependencies, limitations и immutable evidence bundle. | Mechanical gate; reviewer назначается независимо от contributors. |
| Reviewer → Replanning | Findings/Decision с basis digest и closure criteria. | Kernel сохраняет obligations; dispatcher создаёт следующий versioned job. |
| Claim ledger → Paper | Eligible claim/observation versions, artifacts, citations, current reviews. | Paper gate перепроверяет актуальность snapshot перед финализацией bundle. |

LLM provider не владеет SQLite-файлом, credentials или правом утверждать собственный результат. Tool output — недоверенный input typed command. Один service отвечает за write admission; worker пишет только в выделенную область и возвращает артефакты. Совместный доступ к blob bytes не должен обходить role-specific allowlist.

## DomainPack и перенос afterlife

Принятый контракт v1 — [ADR 0016](decisions/0016-domain-pack-contract.md): static manifest, явный реестр, hooks с typed envelopes и `StatisticalReport` v1; силу claim ограничивает ядро. Шаг 10 провёл `synthetic_causal_v1`, `afterlife_seed_v1` и `tabular_classification_v1` одним кодом ядра до synthetic fixture review и внутреннего paper scaffold. `scientific_validity` остаётся `not_assessed`. Отказ чтения hidden input показан только у пакета, который такой вход объявил. Ниже — исходная постановка, которую этот ADR конкретизирует. Минимальный DomainPack предоставляет config schema, input/result schemas и units, планируемые outputs, runner recipe, metric recomputation, interpretation cautions, domain gates и reproduction comparator. Контракт должен выражать cached replay, rerun, reanalysis и new-data replication раздельно. Core знает режим и зависимости, но не знает конкретные поля temperature/window/tokenizer.

Afterlife pack размещает provider/model revisions, protocol/context semantics, stage import, degeneracy controls, trajectory readers и figure adapters. Импорт read-only: оригинальный checkout и его runs не меняются. `PLAN`/`REPORT` преобразуются в historical protocol/claim candidates с локаторами; legacy timestamps не создают preregistration задним числом. Manifest hashes верифицируются, failed/superseded records сохраняются, re-import того же snapshot идемпотентен. Скопированные MIT utilities сохраняют attribution и notice.

Синтетический pack нужен для быстрой инженерной проверки с известным ответом. Он не должен содержать скрытые oracle answers в context агента. Evaluation oracle и holdout inputs физически находятся в отдельном evaluator workspace; обычный `evaluation/` содержит только доступные harness интерфейсы и опубликованную методику.

## Данные конкретного исследования

Рабочие state directories задаются `--root` и отделены от исходного кода framework. Текущий `Store` использует `state.sqlite3` и `artifacts/sha256/<digest>`; остальные элементы ниже появляются по мере реализации. SQLite/WAL и credentials не должны попадать в Git вместе с исходниками.

```text
<study-root>/
├── state.sqlite3                 authoritative events, затем projections/ledger
├── artifacts/sha256/<digest>     immutable inputs, outputs, code и manifests
├── events.jsonl                  текущий v0.1 export
├── review-bundle.json            текущий v0.1 export
├── report.md                     текущий v0.1 export
├── paper-<id>.md / .json          внутренний scaffold после review, v0.1
├── workers/<attempt-id>/         disposable writable workspace, M2
├── exports/<snapshot-id>/        JSONL, review bundle, manuscript, M5
└── checkpoints/                  ссылки на внешний trusted checkpoint, M2
```

Hash-addressed blob не хранит право доступа внутри имени: service проверяет assignment и разрешённый список digest. Экспорт снимается с согласованного snapshot, содержит только разрешённый closure и проверяется при чтении. JSONL/Markdown — производные формы; редактирование отчёта не изменяет факт в state. Garbage collection orphan blobs проектируется отдельно от удаления исторического evidence и не запускается до проверки reachability из всех сохранённых snapshots.

Backup использует согласованный SQLite snapshot и замыкание ссылок на blobs, а не копирование одного DB-файла при активном WAL. Recovery проверяет chain/checkpoint, leases, реальные handles jobs и reservations. Отсутствие наблюдаемого результата ещё не даёт права повторно оплачивать неизвестный API call.

## Проверки по границам

| Область | Что подтверждает готовность |
|---|---|
| State и schemas, M1 | Round-trip/migration/replay, invalid references, conflicting writers, duplicate commands и descendant invalidation. |
| Runner и budget, M2 | Real subprocess/container integration, manifest closure, resource limits, denied file/network access, crash/reconciliation и concurrent reservations. |
| Search, M3 | Ballot replay, deterministic selection, no omitted negative nodes, bounded frontier, stale-state conflicts; отдельно oracle-quality выбранного теста. |
| Independence и loop, M4 | Неудачная попытка чтения закрытого artifact, contributor assignment rejection, independently produced analysis, review-driven actual second experiment. |
| Domain portability, M4 | Одна command boundary для synthetic и afterlife, verified/idempotent import, сохранённый scientific scope. |
| Paper, M5 | Все empirical anchors разрешаются, изменённый artifact/stale review блокируют release bundle, figures/tables rebuild и визуальный QA. |
| Evaluation, M6 | Equal-budget frozen baseline/абляции, hidden oracle, preregistered анализ, полные outcomes, независимый review и bounded conclusions. |

Тесты обязаны проверять обещанную границу, а не только её название в manifest. Evidence для принятия этапа — текущий код, фактический run и проверенный bundle соответствующего масштаба. Документирование будущего каталога или зелёный тест плоского прототипа не завершает следующий этап автоматически.
