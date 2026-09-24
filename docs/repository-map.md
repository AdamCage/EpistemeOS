# Карта репозитория EpistemeOS

Дата: 24 сентября 2026. Здесь отдельно описаны существующие файлы v0.1 и проектируемые модули. Архитектурные решения — в [architecture.md](architecture.md), зависимости и приёмка — в [mvp-plan.md](mvp-plan.md). Названия будущих каталогов задают границы ответственности; пустые пакеты ради этой схемы создавать не требуется.

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
│   ├── decisions/                command, workflow и agent proposal ADRs
│   └── research/
│       ├── afterlife-audit.md
│       ├── ai-scientist-coscientist.md
│       ├── kosmos-virtual-lab-robin.md
│       └── evaluation-plan.md
├── src/episteme/
│   ├── __init__.py
│   ├── __main__.py               python -m episteme
│   ├── cli.py                    demo / inspect / agent / execution / batch / graph / command / backup / restore
│   ├── store.py                  SQLite events/receipts и content-addressed blobs
│   ├── recovery.py               согласованный backup и verified restore в новый каталог
│   ├── commands.py               versioned allowlist, type/role admission, normalization
│   ├── protocols.py              immutable statistical declarations и validation
│   ├── agents.py                 два versioned proposal tasks, durable transitions/provenance
│   ├── agent_controller.py       one-shot dispatch/reconcile/advance вне SQL
│   ├── agent_profiles.py         append-only hypothesis/experiment profile registry
│   ├── agent_proposals.py        frozen v1 prompt, schemas и semantic validator
│   ├── experiment_proposals.py   frozen experiment prompt/schema и validator
│   ├── codex_provider.py         fingerprinted CLI adapter и frozen wrapper
│   ├── planning.py               версии ResearchQuestion/ExplanationSet, bindings и ancestry
│   ├── execution.py              atomic job admission, one-shot dispatch, verified reconciliation
│   ├── batch.py                  frozen primary/reanalysis roster, attempt ownership и settlement
│   ├── batch_controller.py       последовательный resume по сохранённым slots
│   ├── execution_authority.py    локальный marker вне backup/CAS, запрет запуска restored batch
│   ├── runner_backend.py         trusted local Python process supervisor, bounded logs и completion
│   ├── claims.py                 типизированный immutable ClaimLink
│   ├── claim_context.py          scope/циклы связей и транзитивный review context
│   ├── kernel.py                 валидируемые научные команды и gates
│   ├── search.py                 persistent tournament и bounded tree policy
│   ├── reporting.py              snapshot export и внутренний paper scaffold
│   ├── graph.py                  типизированная read-only проекция и queries
│   ├── domains/afterlife.py      bounded historical inspection/import
│   ├── domains/synthetic_causal.py  synthetic fixture recipe и runner sources
│   └── demo.py                   два фиксированных CPU-приложения
├── schemas/                      command, statistical design, question/set, claim link, review и experiment proposal
├── examples/model_hypotheses.py  подготовка одного задания; --execute явно вызывает модель
├── examples/model_experiment.py  frozen synthetic experiment request; --execute явно вызывает модель
└── tests/
    ├── test_kernel.py            инварианты ядра и исторические failure cases
    ├── test_cli.py               реальные CLI/subprocess интеграции
    ├── test_search.py            ballots, tree bounds, reservations и replay
    ├── test_reporting.py         snapshots, review input и paper eligibility
    ├── test_graph.py             typed refs, graph traversal и corruption
    ├── test_afterlife.py         historical import, limits и idempotency
    ├── test_commands_store.py    receipt integrity, atomicity, competing writers и crash
    ├── test_commands_service.py  versioned dispatch, historical replay и real CLI
    ├── test_protocols.py         typed statistical declarations
    ├── test_planning.py          immutable revisions, exact refs, scope, exclusions и head races
    ├── test_planning_workflow.py protocol/review/study/graph binding, CLI и recovery
    ├── test_execution.py         реальные jobs, controller crash, concurrency, Graph/CAS/backup
    ├── test_batch.py             полный roster, failures/unknown, atomicity, restore guard и CLI
    ├── test_execution_authority.py atomic marker, concurrency, corruption и restore
    ├── test_runner_backend.py    реальные descendants, timeout, capture cap и duplicate delivery
    ├── test_claims.py            shape и hashes immutable claim links
    ├── test_claim_context.py     scope, direction, cycles и context closure
    ├── test_claim_workflow.py    review propagation, veto, supersession, paper и CLI replay
    ├── test_claim_lineage.py     последовательные версии и current replacement frontier
    ├── test_recovery.py          exact state/receipt replay, corruption и recovery CLI
    ├── test_recovery_concurrency.py snapshot при append, no-overwrite race, manifest/binding corruption
    ├── test_scientific_workflow.py exposure timing, mode, amendments и review invalidation
    ├── test_cli_recipe.py        host binding через реальный CLI без событий/model call
    ├── test_experiment_agents.py atomic proposal application, stale/replay/recovery
    ├── test_experiment_graph.py  typed refs, schema export и tamper rejection
    ├── test_experiment_proposals.py strict schema и frozen hypothesis order
    ├── test_synthetic_causal.py  fixture recipe, estimates и real runner
    └── test_workflow.py          фактический search → execution → evidence demo
```

`search.py` — самостоятельный модуль над существующим Store/Kernel. Его наличие не превращает CLI в автономный scheduler. README, packaging и CI описывают запуск и проверенные платформы; они не являются scientific evidence.

| Файл | Реальная ответственность | Граница |
|---|---|---|
| `agents.py`, `agent_controller.py`, `codex_provider.py` | Frozen hypothesis/experiment requests, bounded local CLI, response retention, atomic hypotheses/set или protocol/tree node application. | Caller-declared actors и trusted OS/account; нет authenticated independence, model-science approval или автоматического запуска предложенного эксперимента. |
| `experiment_proposals.py` | Frozen prompt, provider schema и strict semantic validator для experiment proposal по исходному порядку hypotheses. | Валидный JSON не доказывает различающую силу эксперимента или реальное исполнение. |
| `domains/synthetic_causal.py` | Синтетический causal recipe, отдельные source для primary/reanalysis и typed exploratory design. | Известный генератор и общие наблюдения для повторного анализа; нет внешнего scientific evidence или доказанной независимости второго анализа. |
| `batch.py`, `batch_controller.py` | Полный roster primary/reanalysis для выбранного scientific node, резерв будущих slots, resume без повторного dispatch, полный технический settlement. | Fresh primary policy; стоимость в attempts, без agent reasoning, automatic analysis/review/replanning. |
| `execution_authority.py` | Локальный token и проверка hash перед изменением execution state batch; DB/CAS restore не получает token. | Не аутентификация и не distributed lease; полное копирование marker владельцем файлов может создать исполняемый clone. |
| `store.py` | Canonical JSON, CAS, verified chain/receipts, atomic command transaction/replay, additive receipt migration и export. | Нет аутентификации, внешнего checkpoint, общего schema migration или distributed storage. |
| `recovery.py` | SQLite online backup, полный наблюдаемый CAS, manifest, semantic closure и restore с точной историей/receipts; эксклюзивный новый destination. | Не переносит процессы/внешнюю среду; filesystem доверенный, нет внешней аутентификации или атомарной видимости всего каталога. |
| `commands.py` | Явный action/role allowlist, strict JSON, аргументы и defaults v1, допуск до handler, historical acknowledgement. | Доверенный local caller; нет внешнего execution или меж-study access boundary. |
| `protocols.py` | Frozen design dataclasses, units/estimand/metrics/splits, mode и structural statistical validation. | Не проверяет actual data, мощность, реальную независимость или uncertainty computation. |
| `planning.py` | ResearchQuestion/ExplanationSet v1, immutable parent refs, heads, hashes/scope, exact exclusion reasons; frozen context с прежними гипотезами. | Constraints и comparison plan декларативны; нет генерации/научной оценки, resource enforcement или actor authentication. |
| `execution.py` | Atomic run/job и result/finalized, unique dispatch, historical receipts, completion identities/hashes, context и CAS closure. | Нет auto-reclaim/retry, signed attestation, study resource ledger или cross-clone exactly-once. |
| `runner_backend.py` | Frozen single Python source/input, отдельный cwd, gated process launch, Windows Job Object / POSIX group, bounded capture, durable completion. | Trusted local profile, без filesystem/network sandbox, package environment reconstruction или domain metric recomputation. |
| `claims.py`, `claim_context.py` | Immutable proposals отношений, validation порядка/scope/циклов; review context из incoming supports/limits и symmetric contradictions/supersession. | Не доказывают научную связь; binding evidence basis и bytes проверяет Kernel/Graph. |
| `graph.py` | Immutable typed nodes/edges, reference closure, ancestor/descendant queries, exact scope filter, JSON/DOT. | Проекция текущих event types, не scientific adjudication или inferred causal graph. |
| `domains/afterlife.py` | Bounded read-only scan, frozen metadata/blob snapshot, сохранение legacy status/dirty/superseded, idempotent import. | Исторические данные не становятся accepted claims; общий runner и metric recomputation не перенесены. |
| `kernel.py` | Hypothesis/protocol/run/result/claim/review commands, правила ролей, binding digests/scope, seeds и run limit, review basis и `next_action`. | Python caller доверенный. Проверка finite metric не пересчитывает науку. `next_action` возвращает решение, не job. |
| `search.py` | `register_tournament`, `pairings`, `ballot`, `ranking`; `register_tree`, `add_node`, `select_next`, `tree_state`, `finish_selection`. Сохраняются policy, ballots, nodes, решения и резервы объявленной стоимости. | Нет LLM judge, worker dispatch/lease или независимого измерения расходов. Priority не продвигает claim и допускает неполное сравнение pool. |
| `demo.py` | Генерация синтетических CSV и два способа OLS в реальных subprocess, локальные actors; завершение перед review. | Только фиксированные программы, без LLM и независимого scientific review. Runtime record не восстанавливает произвольную среду. |
| `reporting.py` | Один snapshot для inspect/export; `PaperBuilder.build` проверяет текущую eligibility и записывает immutable Markdown/JSON+paper event; `materialize` повторно проверяет basis и bytes. | Внутренний scaffold, без полноценного literature/figures/Methods validation или venue formatting; свободный claim text не сертифицируется. |
| `cli.py` | Demo/inspection/gates/export, review JSON, paper scaffold; `agent recipe --input` замораживает host-owned binding, `agent advance` продолжает оба proposal tasks. | Actor ID задаётся доверенным caller; recipe command не вызывает модель, а application не выбирает и не исполняет experiment node; sandbox и независимого scientific review нет. |
| `tests/test_kernel.py` | Протокол до run, источники evidence, scope, budgets при конфликте writers, retention failures, stale review, self-review, integrity. | Unit tests не доказывают clean-room, sandbox, научную правильность или публикационное качество. |
| `tests/test_search.py`, `tests/test_reporting.py`, `tests/test_cli.py` | Поиск и cost reservations, snapshot consistency и paper eligibility, входные review JSON и запускаемые CLI/subprocess сценарии. | Покрытие конкретных failure cases не означает общего доказательства безопасности либо работы независимых научных агентов. |
| `docs/research/*` | Проверяемые основания решений и заранее предлагаемый evaluation design. | Литературный обзор и локальный code audit не означают независимого запуска внешних систем. |

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

Минимальный DomainPack предоставляет config schema, input/result schemas и units, планируемые outputs, runner recipe, metric recomputation, interpretation cautions, domain gates и reproduction comparator. Контракт должен выражать cached replay, rerun, reanalysis и new-data replication раздельно. Core знает режим и зависимости, но не знает конкретные поля temperature/window/tokenizer.

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
