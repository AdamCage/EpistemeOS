# ADR 0016 — контракт DomainPack

Дата: 4 октября 2026. Статус: **принято 4 октября 2026, реализуется поэтапно.** Исполнение хуков с 5 октября 2026 описывает [ADR 0024](0024-pack-hook-subprocess.md): subprocess того же пользователя, не процесс ядра и не sandbox. Пользователь принял все пять рекомендаций; они записаны в разделе «Решения по открытым вопросам». Что из плана уже реализовано и проверено, а что только запланировано, указано в разделе «Ход реализации»; результаты проверок — в [validation.md](../validation.md). Проверки в разделах «Этапы реализации» и «Критерии универсальности» остаются критериями приёмки; выполненными считаются только те, что перечислены в «Ходе реализации». Основания: [архитектура](../architecture.md) (§1, §6, §9), [MVP-план](../mvp-plan.md) (M4: два domain packs, «общий kernel проходит оба pack без проверок по имени afterlife»), [карта репозитория](../repository-map.md) (раздел «DomainPack и перенос afterlife»), ADR [0002](0002-statistical-design.md), [0005](0005-local-runner.md), [0006](0006-execution-batches.md), [0007](0007-model-proposals.md), [0008](0008-experiment-proposals.md), [0013](0013-batch-analysis-admission.md), [0014](0014-manual-domain-binding.md), [0015](0015-afterlife-historical-pilot.md). Предложение писалось на `41ad0e7`, когда ADR 0015 и модули `afterlife_seed*` ещё менялись; после их фиксации в `483f1bd` ссылки на них сверены с этим commit.

## Проблема

Архитектура обещает подключаемые domain packs с config schema, runner, result schema, пересчётом метрики, domain gates, interpretation cautions и reproduction comparator. На `41ad0e7` вместо единого контракта есть несколько частичных швов с разными правилами.

- Модельный путь [ADR 0008](0008-experiment-proposals.md) привязан к одному домену внутри модулей ядра. [`experiment_proposals.py`](../../src/episteme/experiment_proposals.py) фиксирует `RECIPE_ID = "synthetic_causal_v1"` и его параметры, а [`agents.py`](../../src/episteme/agents.py) импортирует `domains.synthetic_causal` и проверяет поля synthetic `world`. Второй домен не пройдёт этот путь без правки ядра.
- [`domain.bind`](../../src/episteme/domain_binding.py) универсален, но recipe для ядра непрозрачен: проверяются форма, CAS bytes и совместимость с runner. Исходник адаптера — один bounded CAS artifact; на практике это bytes одного файла.
- Анализ вызывает [`AnalysisAdapter`](../../src/episteme/analysis_controller.py) с `adapter_id`, `adapter_version` и `propose(store, state)` и принимает proposal schema v1 со свободным `details`. Адаптер получает весь `Store` и использует приватные функции ядра (`Kernel._get`, `domain_binding._index`). То, что synthetic анализ не читает скрытый `world`, проверяет тест с подменой `Store.read`, а не входной контракт.
- Pinning непоследователен. При ручной привязке ID, версия и исходник адаптера фиксируются до batch. На модельном пути [`analysis.apply`](../../src/episteme/batch_analysis.py) сверяет только `compiled.domain == adapter_id`, поэтому версия и код анализа выбираются после исполнения. Controller сохраняет `inspect.getfile(type(adapter))` — один файл без импортируемых модулей пакета.
- Код анализа выбирает caller. На `41ad0e7` CLI вызывал только synthetic adapter; с `483f1bd` (ADR 0015) он выбирает один из двух адаптеров флагом `--adapter`, по умолчанию synthetic.
- Смысл roster не объявлен. Ядро знает только целые `seeds`; afterlife pilot использует их как индексы уже наблюдённых траекторий, а таблица paper называет колонку `Seed`. Synthetic recipe задаёт `sample_size` на один seed, afterlife pilot — общее число траекторий.
- Статистика не структурирована. Оба адаптера возвращают только `inconclusive`/`exploratory`, а отсутствие interval, power и significance записано в limitations свободным текстом. [ADR 0002](0002-statistical-design.md) замораживает декларации, но сверки с фактическим анализом и правила, связывающего отсутствие статистики с силой claim, нет.
- Профиль исполнения узкий. [`trusted_local_python_v1`](0005-local-runner.md) запускает `python -I -S program.py input.dat --seed N`: один stdlib-файл, один input, наследуемое окружение, без sandbox. Пакет, которому нужны сторонние библиотеки, GPU или сеть, сейчас исполниться не может. Контракт не должен это скрывать.

## Решение

Вводится **DomainPack contract v1**: статический manifest, явный реестр, hooks с типизированными JSON envelopes и стандартный statistical report. Пакет предлагает и вычисляет; ядро допускает переходы. Действует правило монотонности: проверка пакета может только добавить отказ или ограничение, но не ослабить правило ядра и не повысить силу claim.

### Разделение полномочий

| Только ядро | Пакет |
|---|---|
| Events, receipts, запись в CAS, expected revision, replay | Каталог параметров и их предметная проверка |
| Preregistration, exposure и `seen_data`, роли data splits, amendments | Дополнительные предметные ограничения protocol |
| Mechanical gate, полнота roster, tolerance повторного анализа | Bytes программ, input и outputs для runner |
| Сила claim: `inference_mode`, допустимый `outcome`, отказ при превышении | Схема raw data и пересчёт метрики |
| Assignment, контекст reviewer, review, obligations, resolution, paper eligibility | Текст bounded claim, limitations, statistical report |
| Режим replication и измерения независимости по фактам | Дополнительные реализации повторного анализа и comparator |
| Бюджет, Search, вызовы provider/model, dispatch, `scientific_validity` | Interpretation cautions; необязательный захват исторических bytes |

### Предложенные hooks и существующие швы

| Hook | Существующий шов | Решение |
|---|---|---|
| `propose_experiment()` | `experiment_proposals.py`, `agents.py`, `synthetic_causal.describe()` | Не hook пакета. Prompt, schema, бюджет вызовов, provider и атомарное применение остаются в ядре. Пакет даёт `describe()`, `validate_parameters()` и `compile_protocol()`. |
| `validate_protocol()` | `_validate` в `compile_recipe`; проверки design в afterlife анализе | Принять как чистый hook. Вызывается при привязке и повторно при `batch.plan` и анализе. |
| `prepare_execution()` | `compile_recipe()`; `compile_seed_bundle()` читает файлы и сам пишет CAS | Разделить на необязательный `capture()` (однократное read-only чтение внешнего источника до привязки) и чистый `compile_execution()`. В CAS пишет только ядро. |
| `validate_outputs()` | `_check_completion` и `_metric` ядра; проверки `raw.json` в адаптерах | Принять как чистый hook по slots; общие проверки ядра остаются. |
| `recompute_metrics()` | `_estimate` synthetic, `_steps` afterlife | Принять. Сравнение с primary и reanalysis metrics по арифметическому допуску выполняет ядро. |
| `analyse()` | `AnalysisAdapter.propose()` | Принять: `AnalysisReport` v2 со `StatisticalReport` v1; потолок силы claim вычисляет ядро. |
| `replicate()` | `reanalysis_implementation` в binding и batch; gate ядра | Отклонить в этой форме. Пакет может поставить дополнительные реализации и `compare()`. Режим выбирает policy protocol, а независимость реализации, авторства, контекста и данных ядро выводит из digests, actors и data bytes. Тот же код на тех же данных — `exact_rerun`; вторая реализация того же пакета — повторный анализ тем же автором, а не независимая реализация. |
| `build_domain_context()` | `describe()` для модели; manifest `review.assign` | Разделить. Пакет даёт каталог и static cautions; состав контекста модели и reviewer и allowlist определяет ядро. Пакет не добавляет bytes в контекст reviewer. |

В предложении недостаёт статического manifest, `roster_semantics`, `numeric_tolerance`, объявления скрытых входов (`hidden_inputs`), `capture`, `execution_profile` и `statistical_capabilities`.

### Идентичность, момент привязки и реестр

`PackManifest` v1 содержит `contract_version=1`, `pack_id` (формат `adapter_id`), `pack_version`, `roster_semantics` (`rng_seed`, `partition_seed`, `frozen_unit_index`, `deterministic_single`), метрики с units, outputs и их schema IDs, `numeric_tolerance`, `hidden_inputs`, `execution_profiles`, `statistical_capabilities` и `interpretation_cautions`. `code_manifest` перечисляет отсортированные относительные пути и SHA-256 **всех** файлов пакета; `pack_code_digest` — SHA-256 его canonical JSON. Файлы сохраняются в CAS с общим лимитом размера, поэтому Graph, backup и review basis охватывают весь код пакета, а не один модуль. Пакет импортирует только stdlib и новый фасад `episteme.domains.api`; импорт приватных функций ядра запрещён и проверяется тестом.

Привязка происходит при preregistration. Ручной путь получает новую команду (предварительно `pack.preregister`), которая в одной receipt компилирует `ProtocolDraft` и `ExecutionPlan`, вызывает `preregister_for_set` и записывает `domain_binding` v2. Модельный путь делает то же внутри `agent.apply_experiment` v3. Привязка фиксирует pack identity, `pack_code_digest`, digests каталога и recipe, `roster_semantics`, execution profile, результат `validate_protocol` и уровень environment closure. `batch.plan` сверяет пакет и recipe. Анализ (предварительно `pack.analyse`) сравнивает bytes загруженных файлов с закреплённым `code_manifest` и отказывает при любом расхождении.

Новая версия пакета требует новой привязки, а значит нового protocol с учётом exposure. Анализ другим кодом после исполнения в v1 закрыт; явный post-hoc переход с потолком `exploratory` требует отдельного решения. Привязки v1 и `agent_application` v2 остаются валидными по прежним правилам и в отчётах помечаются `pack_pinning=legacy_single_file`; задним числом ничего не закрепляется. `domain.bind` и `analysis.apply` v1 сохраняются для legacy и replay.

Реестр — явный allowlist в репозитории (`episteme/domains/registry.py`: `pack_id → module`). Добавить пакет — значит изменить код и пройти review. Entry points и автоматический discovery в v1 не используются: они незаметно расширяют круг кода, исполняемого в процессе ядра. CLI берёт пакет из привязки protocol; флаг выбора адаптера может остаться только как утверждение, которое обязано совпасть с привязкой.

### Граница доверия

Пакет — доверенный локальный Python под той же OS identity. Hooks исполняются в процессе ядра, программы из `ExecutionPlan` — в общем runner без sandbox. Пакет может читать Store, файлы и сеть, ошибиться или солгать. Pinning, повторные проверки и монотонность выявляют случайный drift и ошибки, но не защищают от злонамеренного кода. Malicious pack, malicious executor и утечка oracle через пакет — вне scope до появления isolation profiles. События привязки и анализа фиксируют `pack_trust=trusted_local_code` и `hook_isolation=in_process`.

Hooks не получают `Store`. Ядро передаёт копии нужных payloads и `CasView` — чтение только по allowlist digests, который ядро вычисляет и записывает в событие. Для анализа в allowlist входят наблюдённые `raw_data` и `metrics`, recipe и каталог, но не `hidden_inputs`; для synthetic это `protocol.data` со скрытым world. Это не security boundary. Зато входы становятся явными и воспроизводимыми, их можно передать будущему изолированному процессу, а тест `test_hidden_world_artifact_is_never_read` превращается в проверку во время исполнения.

### Чистота и детерминизм

| Hook | Когда вызывается | Требование | Проверка ядром |
|---|---|---|---|
| `manifest`, `describe` | Загрузка пакета и каждая привязка | Константа версии; canonical JSON без NaN, ограниченный размер | Сравнение с закреплённым digest |
| `validate_parameters`, `validate_protocol` | Привязка, `batch.plan`, анализ | Чистые, без I/O | Повторный вызов перед каждым из этих переходов |
| `capture` | Только до привязки | Read-only чтение объявленного внешнего пути, без сети; одинаковые исходные bytes дают одинаковый результат | При replay не вызывается; дальше используется только CAS |
| `compile_protocol`, `compile_execution` | Привязка, модельное применение | Чистые над recipe, каталогом и захваченными bytes | Повторная компиляция и сравнение digests с protocol |
| `validate_outputs`, `recompute_metrics`, `analyse` | Анализ | Чистые над `CasView`: без времени, сети, environment и неявной случайности | Read-only `pack verify` повторяет вызовы и сравнивает canonical bytes |

Hooks выполняются вне SQL write transaction на зафиксированном snapshot, как сейчас `propose` вызывается до `analysis.apply`. Внутри команды ядро повторно проверяет закреплённый код, актуальность snapshot и settlement, envelopes, вычисляемые им поля и потолок силы claim; внешняя работа и длительные вычисления внутри `Store.command` не выполняются. Float между разными interpreter fingerprints сравнивается по `numeric_tolerance` — это допуск арифметики, а не неопределённость.

### Typed envelopes

Все envelopes — JSON с `schema_version`, точным набором полей и лимитами размера, без duplicate keys и non-finite numbers, как действующие validators. JSON Schemas публикуются, runtime-проверка остаётся без зависимостей.

- `ParameterCatalog`: видимые planner и модели параметры, метрика, outputs и limitations; без скрытых входов.
- `ProtocolDraft`: design, analysis plan, metric, stopping rule, typed `StatisticalDesign` v1, roster, run limit и `roster_semantics`.
- `ExecutionPlan`: bytes primary и reanalysis программ, input, outputs, wall time, лимит bytes, capabilities, `execution_profile`, `environment_requirements`.
- `CaptureBundle`: внешний источник, inventory путей и SHA-256, итог аудита и bytes.
- `OutputCheck` по slot и `Recomputation` по единице roster: пересчитанное значение, обе записанные метрики, абсолютные расхождения и допуск.
- `AnalysisReport` v2: statement, limitations, предлагаемые `outcome` и `inference_mode`, ограниченные `details` и ссылка на `StatisticalReport` v1. Report — отдельный CAS artifact; он входит в evidence basis через событие анализа.

### StatisticalReport v1

Все 12 полей обязательны и имеют один из статусов: `supplied` с `value`; `not_supplied` с непустой `reason`; `not_applicable` с `reason`, допустимый только если неприменимость следует из preregistered design. Отсутствующее поле, пустая причина или `not_applicable` вопреки protocol отвергают анализ целиком. Ядро ничего не дополняет и не переписывает молча.

| Поле | `value` при `supplied` | Источник истины | Проверка ядром |
|---|---|---|---|
| `estimand` | Hash protocol и текст | Preregistered estimand | Точное совпадение; иная цель — только как deviation |
| `estimator` | Имя, описание, hook | Пакет | Обязательно `supplied` |
| `point_estimate` | Метрика, unit, значение, при необходимости по единицам roster | Пакет | Обязательно; метрика и unit preregistered; согласовано с `recompute_metrics` |
| `uncertainty` | Метод, resampling unit | Protocol и пакет | Метод совпадает с preregistered, иначе deviation; `not_applicable` только при preregistered `not_applicable` |
| `confidence_interval` | Уровень, границы, метод | Пакет | Конечные границы, `lower ≤ upper`; N/A — по тому же правилу |
| `effect_size` | Мера, значение, точка сравнения | Пакет | Конечное значение |
| `assumptions` | Список: допущение, `holds`/`violated`/`unchecked`, ссылка | Пакет | Непустой список |
| `sample_size` | Unit, `unit_scope` (`per_slot`/`total`), planned, analysed, exclusions, отсутствующие slots | Protocol, settlement, пакет | Planned и отсутствующие slots вычисляет ядро; расхождение — отказ |
| `multiple_testing` | Family, позиция claim, correction, скорректированный результат | Protocol и пакет | Family и correction равны preregistered |
| `stopping_rule` | Hash правила, полнота roster, interim looks | Protocol и settlement | Полноту roster вычисляет ядро |
| `sensitivity_analysis` | Список: имя, preregistered или post hoc, результат | Пакет | Post-hoc анализ силу не повышает |
| `deviations` | Список: поле, план, факт, причина | Пакет и ядро | Ядро добавляет найденные расхождения |

Фрагмент synthetic report; остальные поля обязательны так же:

```json
{
  "schema_version": 1,
  "protocol_hash": "<sha256 protocol event>",
  "point_estimate": {"status": "supplied",
    "value": {"metric": "treatment_effect", "unit": "outcome units", "value": 2.0}},
  "confidence_interval": {"status": "not_applicable",
    "reason": "Preregistered uncertainty.method is not_applicable; point estimate only."},
  "assumptions": {"status": "not_supplied",
    "reason": "Balance and positivity checks are not computed by this pack."}
}
```

### Потолок силы claim

В `claims.py` уровней силы нет: там только декларации связей между claims. Сила claim в ядре — пара `inference_mode` (`unclassified`, `descriptive`, `exploratory`, `confirmatory`) и `outcome` (`supports`, `refutes`, `inconclusive`); `Kernel._inference_mode` уже требует confirmatory protocol для `confirmatory`. Ядро вычисляет потолок до записи claim и сохраняет его с причинами в событии анализа:

1. Если `estimator`, `point_estimate` или `sample_size` не `supplied`, анализ отвергается.
2. `supports` и `refutes` требуют `supplied` `confidence_interval` и `effect_size`. Исключение — `descriptive` protocol с preregistered `uncertainty.method=not_applicable`: вывод относится только к проанализированным единицам, режим остаётся `descriptive`. В остальных случаях допустим только `inconclusive`.
3. Любое deviation, не предусмотренное protocol, ограничивает режим уровнем `exploratory`. Post-hoc sensitivity analyses силу не повышают.
4. `confirmatory` дополнительно требует: все поля `supplied` или согласованно `not_applicable`, пустой `deviations`, analysed = planned минус preregistered exclusions, полный roster и отсутствие допущений со статусом `unchecked` или `violated`.
5. Расхождение полей, которые ядро вычисляет само, приводит к отказу.
6. Предложение выше потолка отвергается целиком, без тихого понижения: statement пишет пакет, и он должен соответствовать outcome. Ядро добавляет в limitations claim строку о каждом `not_supplied` поле и каждом deviation.

Потолок механический. Он не меняет `scientific_validity=not_assessed`, не заменяет review и не мешает reviewer потребовать сужения. Оба существующих пакета по этим правилам получают то же, что выдают сейчас: `exploratory` и `inconclusive`. Меняется то, что отсутствие статистики становится структурированным и проверяемым.

### Связь с ADR 0002

| Замораживается до данных (ADR 0002) | Сообщается после анализа (этот ADR) |
|---|---|
| Mode, experimental unit, estimand, metrics и units, sample size и rationale, метод и unit uncertainty, exclusions, stopping rule, family и correction multiple testing, data splits | Estimator, оценки, interval, effect size, проверки допущений, фактически проанализированные и исключённые единицы, применённая correction, полнота roster и interim looks, sensitivity analyses, deviations |

`StatisticalDesign` v1 не меняется. Report ссылается на hash protocol и не переопределяет preregistered поля: значение либо совпадает, либо попадает в `deviations`. Это первый узкий срез сверки planned-versus-observed из M1: ядро сравнивает число единиц, полноту roster и методы, а правильность вычислений остаётся задачей независимой реализации и review. Поле `unit_scope` устраняет нынешнюю неоднозначность «на seed» или «всего». Дизайны с зависимыми оценками, например repeated cross-validation, упираются в правило v1 «resampling unit равен experimental unit»; это кандидат на `StatisticalDesign` v2, а не часть данного решения.

### Место для execution profile и environment closure

- `execution_profile`: реализован только `trusted_local_python_v1`. Неподдерживаемый профиль отвергается при привязке, как требует ADR 0005.
- `environment_requirements` резервирует `closure_level` (сейчас только `interpreter_fingerprint`), `image_digest`, `lock_digest`, `accelerator`, `network_policy` и `env_allowlist`. В v1 все поля, кроме текущего уровня, обязаны быть `null`; иное значение отвергается, пока профиль не реализован.
- `hook_isolation` резервирует `subprocess` и `container` для будущих isolation profiles.
- Все bytes `ExecutionPlan` остаются в CAS, поэтому будущий `episteme reproduce <run-id>` в режиме `exact_rerun` сможет исполнить их без загрузки кода пакета. Здесь он не проектируется.
- Literature/novelty и controller loop не проектируются: пакет не делает novelty claims и не выбирает следующее действие.

## Миграция

1. **`synthetic_causal_v1`.** Фасад над `synthetic_causal` и `synthetic_batch_analysis` без изменения вычислений: `roster_semantics=rng_seed`, `protocol.data` со скрытым world объявлен в `hidden_inputs`, `numeric_tolerance=1e-9`. В report `uncertainty` и `confidence_interval` — `not_applicable` по preregistered design, `assumptions` — `not_supplied`. Итог остаётся `exploratory`/`inconclusive`. Истории с привязкой v1 и `agent_application` v2 проходят replay без изменений.
2. **Модельный путь ADR 0008.** `agent.request_experiment` v3 с `pack_id` и digest каталога; schema `experiment-proposal-v2`, в которой `parameters` проверяет пакет. Ядро перестаёт импортировать `synthetic_causal`; для replay запросов v2 сохраняется замороженный legacy compiler.
3. **`afterlife_seed_v1`** — после фиксации ADR 0015 (зафиксирован в `483f1bd`). Аудит исторического run и запись bytes разделяются на `capture()` и чистую повторную проверку захваченного inventory по CAS. `roster_semantics=frozen_unit_index`. Проверка `seen_data` и open discovery split переходит из анализа в `validate_protocol` при привязке. В report interval и effect size — `not_supplied`: шаги внутри траектории не независимы, данные относятся к одной model/configuration. Historical importer `afterlife.py` остаётся отдельным namespace `historical_unverified` вне evidence path.
4. **Кандидат третьего пакета — `tabular_classification_v1`.** Только CPU, детерминированно и только stdlib. Это требование, а не стиль: `dependencies = []`, а runner запускает Python с `-S`. Пакет сравнивает два классификатора на чистом Python (например, majority baseline и logistic regression с фиксированным числом шагов gradient descent) на frozen CSV.
   - Exploratory фаза: repeated stratified k-fold на development split, `roster_semantics=partition_seed`. Повторы делят одни данные, поэтому seeds не являются независимыми выборками; report обязан указать это в `assumptions`.
   - Confirmatory фаза: один прогон на ранее не открытом holdout split с preregistered парным сравнением на одних и тех же test examples (например, exact McNemar и interval парной разности accuracy, вычислимые через `math`). Этот пакет первым проверяет положительную ветвь потолка: confirmatory claim возможен только при полном report и непросмотренном holdout.
   - `validate_protocol` запрещает exploratory protocol с bundle, содержащим holdout bytes: ядро учитывает exposure по digests, а не по содержимому.
   - CI использует детерминированно сгенерированную fixture-таблицу, явно помеченную synthetic. Пилот на публичной таблице (например, небольшом наборе UCI) — отдельный ручной запуск после проверки лицензии и закрепления digest при `capture`.

## Этапы реализации

Каждый шаг — отдельный commit с тестами и обновлением этого ADR, command API и validation.md. Перед commit, затрагивающим storage или интерфейсы, выполняются `python -m unittest discover -s tests -v` и соответствующие CLI integration checks.

1. `domains/api.py`: envelopes и `StatisticalReport` v1 как неизменяемые dataclasses, JSON Schemas; unit-тесты на пропуск поля, пустую причину и `not_applicable` вопреки design.
2. `domains/registry.py`, `code_manifest` и загрузчик со сверкой bytes; общий conformance suite для любого зарегистрированного пакета.
3. Фасад synthetic pack. Golden test: basis hashes и Graph snapshot сохранённой fixture history не меняются.
4. `pack.preregister` и `pack.analyse` в ядре: pinning, `CasView`, проверки и пересчёт под контролем ядра, report artifact, потолок силы claim, автоматические limitations; поддержка Graph, recovery, export и review basis. Тесты на drift кода, чужую версию, пропущенное поле, превышение потолка, restart между receipts, backup/restore и CLI.
5. CLI определяет пакет по привязке; read-only `pack describe` и `pack verify`.
6. Фасад afterlife pack после фиксации ADR 0015 (зафиксирован в `483f1bd`).
7. Reporting и export показывают report и каждое `not_supplied`; колонка roster подписывается по `roster_semantics`; политика `blind_initial_review_v2` (решение по вопросу 4 принято 4 октября 2026).
8. Третий пакет на fixture-данных; ручной пилот отдельно.
9. Обобщение модельного пути ADR 0008.
10. Приёмка универсальности по критериям ниже; запись результатов в validation.md.

Условная оценка — 3–5 инженерных недель. Шаги 1–6 дают работающий контракт для двух пакетов примерно за половину этого срока.

## Что означает заморозка ядра

- Изменения ядра ограничены швом пакета: новые версионированные actions и события, их поддержка в Graph, recovery и export, исправления ошибок.
- Payloads и hashes существующих event kinds, recipe basis для histories без привязок v2, семантика gate, review, obligations, resolution и assignment не меняются. Golden test пересчитывает basis и Graph snapshot сохранённой fixture history до и после каждого commit.
- Опубликованные v1 signatures и actions сохраняются; новое добавляется только версиями.
- Вне шва ничего не добавляется: ни controller loop, ни literature layer, ни новые agent personas, web UI или graph DB.
- В модулях ядра нет проверок по `pack_id` и импортов конкретных пакетов, кроме реестра; это проверяет статический тест.

## Критерии универсальности

Для каждого зарегистрированного пакета одинаково выполняются:

1. Один и тот же код ядра; статический тест не находит доменных имён и импортов пакетов вне `domains/` и реестра.
2. Conformance suite: manifest и `code_manifest`, детерминизм hooks, отсутствие доступа к `Store`, чтение только из allowlist, валидные envelopes и все 12 полей report.
3. Путь привязка → `batch.plan` → `batch advance` → анализ → `review.assign` → явно synthetic fixture review → paper scaffold; restart на каждой границе receipt без дублей; backup/restore; Graph closure включает код пакета, recipe и report.
4. Одинаковые отказы tamper suite: изменён byte raw output; изменён файл пакета после привязки; подставлена другая версия; удалено поле report; `not_applicable` вопреки protocol; предложение выше потолка; попытка прочитать hidden input.
5. Одинаковое применение потолка: synthetic и afterlife остаются `exploratory`/`inconclusive`; положительную confirmatory ветвь проверяет третий пакет.
6. `roster_semantics` различается и правильно отображается; режим replication и измерения независимости выводит ядро.
7. Схемы raw data действительно разные, а не переименованный synthetic пример, как требует MVP-план.

## Решения по открытым вопросам

4 октября 2026 пользователь принял все пять рекомендаций предложения. Формулировки вопросов и рекомендаций сохранены.

1. Реестр: явный allowlist или entry points. Рекомендация — allowlist до появления isolation profiles. **Решение: явный allowlist, entry points не используются.**
2. Превышение потолка: отказ или тихое понижение. Рекомендация — отказ. **Решение: предложение выше потолка отвергается целиком, без тихого понижения.**
3. Анализ другой версией пакета после исполнения. Рекомендация — в v1 закрыть; post-hoc переход с потолком `exploratory` проектировать отдельно. **Решение: в v1 запрещено; post-hoc путь с потолком `exploratory` — отдельный будущий design.**
4. Получает ли reviewer statistical report в начальном контексте. Сейчас `blind_initial_review_v1` исключает analysis proposals и исходник адаптера. Рекомендация — `blind_initial_review_v2` с report, но без исходника пакета и `details`; assignments v1 не меняются. **Решение: принято; реализация относится к шагу 7 и в текущий срез (шаги 1–6) не входит. До неё assignments остаются `blind_initial_review_v1`, и report в начальный контекст reviewer не попадает.**
5. Когда обобщать модельный путь ADR 0008. Рекомендация — после шагов 1–6, когда контракт подтверждён на двух analysis packs. **Решение: только после того, как контракт работает для двух packs (шаг 9, вне текущего среза).**

## Ход реализации

Шаги 1–6 и 8–10 реализованы. Шаг 7 в части review закрыл [ADR 0018](0018-claim-families-and-review-admission.md); отображение `roster_semantics`, которого в том ADR не было, сделано в шаге 10 на уже записанных полях. Медленная повторная проверка receipts в `analysis advance` здесь не исправляется.

| Шаг | Состояние |
|---|---|
| 1. Envelopes, `StatisticalReport` v1, JSON Schemas | Реализован и локально проверен в `c5270ac`: [`domains/api.py`](../../src/episteme/domains/api.py), девять schemas в [`schemas/`](../../schemas), `tests/test_domain_api.py`. Ядро эти envelopes пока не вызывает. |
| Golden test старых histories | Реализован в `c5270ac`, раньше шага 3: три synthetic fixture histories, созданные немодифицированным кодом `483f1bd`, и их basis, gates, Graph, export bundle и replay (`tests/test_golden_history.py`). |
| 2. Реестр, `code_manifest`, conformance suite | Реализован и локально проверен в `0acd593`: [`domains/registry.py`](../../src/episteme/domains/registry.py), schema `pack-code-manifest-v1`, загрузчик, исполняющий ровно хешированные bytes, статическая проверка импортов и `tests/test_domain_pack_conformance.py` с тестовым пакетом `conformance_fixture_v1`. Hook-facing типы `CompileRequest`, `ProtocolContext`, `AnalysisContext` и `CasView` добавлены в `domains/api.py`. |
| 3. Фасад `synthetic_causal_v1` и golden test | Реализован и локально проверен в `e70cfb8`: пакет [`domains/packs/synthetic_causal_v1`](../../src/episteme/domains/packs/synthetic_causal_v1) зарегистрирован, проходит conformance suite; `tests/test_synthetic_pack.py` сравнивает его с legacy compiler и legacy adapter. |
| 4. `pack.preregister`, `pack.analyse`, потолок, `CasView`, Graph/recovery/export | Реализован и локально проверен в `89a44c6`: [`domain_packs.py`](../../src/episteme/domain_packs.py), события `pack_binding` и `pack_analysis`, их поддержка в basis, gate, `batch.plan`, review assignment, Graph, export и backup/restore; `advance_pack_analysis` в `analysis_controller.py`; `tests/test_pack_workflow.py` и `tests/test_pack_universality.py`. |
| 5. CLI по привязке, `pack describe`, `pack verify` | Реализован и локально проверен в `a1ae131`: `episteme analysis advance` берёт пакет или legacy adapter из привязки protocol, `--adapter` стал необязательным утверждением; read-only `episteme pack describe PACK_ID` и `episteme pack verify --root ROOT`; `tests/test_pack_cli.py`. |
| 6. Фасад `afterlife_seed_v1` | Реализован и локально проверен: пакет [`domains/packs/afterlife_seed_v1`](../../src/episteme/domains/packs/afterlife_seed_v1) зарегистрирован и проходит conformance suite; `episteme pack capture` сохраняет захваченные bytes в CAS без событий; `tests/test_afterlife_pack.py`. На восстановленной копии реального пилота ADR 0015 пакетный путь дал те же девять долей и тот же statement, что legacy claim (validation.md). |
| 8. `tabular_classification_v1` | Реализован и локально проверен: пакет [`domains/packs/tabular_classification_v1`](../../src/episteme/domains/packs/tabular_classification_v1) в явном allowlist, весь каталог закрепляется `code_manifest`, профиль `trusted_local_python_v1`. Путь `pack.preregister` → batch → `pack.analyse` → `review.assign` на сгенерированной таблице проходит до назначения reviewer. Verdict и paper не создаются. Числа — в validation.md. |
| 9. Обобщение модельного пути ADR 0008 | Реализован и локально проверен. `agent_request` schema 3 и конверт `experiment-proposal-v2`: пакет объявляет схему параметров и компилирует принятое предложение, ядро записывает protocol, `pack_binding` и узел. `agents.py` и `experiment_proposals.py` не импортируют конкретный пакет. Schema 2 и golden histories не переписывались. Числа — в validation.md. |
| 10. Приёмка универсальности | Реализована и локально проверена с записанными пробелами. Для `synthetic_causal_v1`, `afterlife_seed_v1` и `tabular_classification_v1` один путь ядра доходит до явного synthetic fixture review и внутреннего paper scaffold там, где `next_action` равен `paper_candidate`. `scientific_validity` остаётся `not_assessed`. `proposal.prepare_next` принимает application schema 3, не меняя defaults schema 2. Числа и открытые пункты — в validation.md и ниже. |

Критерии универсальности для трёх зарегистрированных пакетов:
- пункт 1 — тот же код ядра. `agents.py`, `experiment_proposals_v2.py`, `pack_proposals.py` и `proposal_execution.py` не импортируют конкретный пакет. В исключениях статического теста остаются `experiment_proposals.py` (замороженный текст `experiment-proposal-v1` называет recipe) и `legacy_experiment.py` (compiler schema 2);
- пункт 2 — conformance suite трёх пакетов, как после шагов 6 и 8;
- пункт 3 — привязка → `batch.plan` → `batch advance` → анализ → `review.assign` → synthetic fixture review через `review.submit` → paper scaffold. Restart между анализом и назначением не пишет дублей; повтор каждой receipt того же пути тоже не пишет дублей. Backup/restore делается на временной копии, не на `.research/afterlife-pilot-20261004`. Graph closure привязки включает код пакета и recipe, closure анализа — report. Paper строится, потому что механический допуск ADR 0018 даёт `paper_candidate`. Manuscript содержит rationale fixture и говорит, что counted approval не является научной оценкой;
- пункт 4 — для всех трёх пакетов отвергаются изменённый byte raw output, изменённый файл пакета после привязки, чужая версия, удалённое поле report, `not_applicable` вопреки design и предложение выше потолка. Отказ «hidden input» есть только у `synthetic_causal_v1`, который объявляет `hidden_inputs=["protocol_data"]`. У двух других пакетов список пуст; чтение digest вне allowlist отвергается, но другой ошибкой. Этот шаг не добавляет скрытый вход, которого пакет не объявил;
- пункт 5 — synthetic и afterlife остаются `exploratory`/`inconclusive`; положительную ветвь потолка по-прежнему проверяет `tabular_classification_v1` на сгенерированной таблице. Fixture approval не превращает этот потолок в scientific review;
- пункт 6 — `rng_seed`, `frozen_unit_index` и `deterministic_single` различаются и подписаны в export и paper. Режим replication и факты независимости выводит ядро (`same_data_reanalysis`, `context=not_established`, actors caller-declared). `partition_seed` на живом протоколе не показан: exploratory-фаза repeated k-fold не реализована (отклонение шага 8);
- пункт 7 — схемы raw data трёх пакетов разные, как после шагов 6 и 8.

### Отклонения от предложения и уточнения шага 1

- **Отклонение.** `unit_scope` принимает `per_roster_unit` или `total` вместо `per_slot`/`total`. В batch slot означает отдельную primary или reanalysis попытку, а `missing_slots` перечисляет именно такие slots; одно слово с двумя смыслами в одном поле report недопустимо.
- **Отклонение.** `sample_size.value` дополнительно содержит `planned_total`: для `per_roster_unit` это `planned` × размер roster, для `total` — `planned`. Ядро сможет вычислить его само и отвергнуть расхождение.
- **Уточнение.** `ProtocolDraft` дополнительно содержит `sample_size_scope`, `replication_tolerance` и `seen_data`. Интерпретация `sample_size` («на единицу roster» или «всего») закрепляется до данных, а не выбирается в report.
- **Уточнение.** Значения полей при `supplied` имеют фиксированные схемы: `estimand` — `{protocol_hash, text}`; `estimator` — `{name, description, hook}`; `point_estimate` — `{metric, unit, value|null, by_roster_unit}`; `uncertainty` — `{method, resampling_unit}`; `confidence_interval` — `{level, lower, upper, method}`; `effect_size` — `{measure, value, reference}`; `assumptions` — непустой список `{assumption, status, reference}`; `sample_size` — `{experimental_unit, unit_scope, planned, planned_total, analysed, exclusions, missing_slots}`; `multiple_testing` — `{family, claim_position, correction, adjusted_result}`; `stopping_rule` — `{rule_sha256, roster_complete, interim_looks}`; `sensitivity_analysis` и `deviations` — списки, пустой список при `supplied` явно означает «нет».
- **Уточнение.** `not_applicable` допустим только так: `uncertainty` и `confidence_interval` — при preregistered `uncertainty.method=not_applicable`; `effect_size` — только в `descriptive` design; `multiple_testing` — при preregistered `correction=not_applicable`; остальные поля — никогда. Это правило проверяет `StatisticalReport.validate_design`.
- **Уточнение.** `PackManifest.roster_semantics` — список поддерживаемых значений, конкретное выбирает `ProtocolDraft` (третьему пакету нужны две фазы); `outputs` — словарь label → `{path, schema_id}`; булево `capture` объявляет hook захвата.
- **Уточнение.** Frozen `ExecutionPlan` и `CaptureBundle` ссылаются на bytes через `{sha256, bytes}`; bytes записывает в CAS только ядро. `CaptureBundle.source.label` выводится пакетом из захваченных bytes, а не из пути файловой системы, чтобы одинаковые bytes давали одинаковый bundle.
- **Уточнение.** Одна декларативная schema на envelope используется и runtime-проверкой без зависимостей, и опубликованным файлом; тест сравнивает их. Validator поддерживает строгое подмножество JSON Schema и отвергает неизвестные ключевые слова, а не пропускает их.

### Отклонения от предложения и уточнения шага 2

- **Отклонение (усиление).** Загрузчик не сравнивает bytes уже импортированного модуля, а сам хеширует все файлы каталога пакета и исполняет именно эти bytes под приватным именем модуля `_episteme_pack_<id>_<digest>`. Окна между хешированием и импортом нет. Изменение файла после загрузки не меняет исполняемый код; следующая загрузка даёт новый digest, и сверка с закреплённым digest отказывает.
- **Уточнение.** Пакет — каталог-package, имя каталога равно `pack_id`; все его файлы, кроме `__pycache__`, обязаны быть `.py`. Установленный wheel содержит только Python-модули, поэтому файл другого типа сделал бы digest исходного и установленного дерева разным. `code_manifest` — schema `pack-code-manifest-v1`: `pack_id` и отсортированный список `{path, sha256, bytes}`.
- **Уточнение.** Статическая проверка импортов разрешает stdlib, кроме модулей времени, случайности, процессов, сети, окружения и динамического импорта (`os`, `sys`, `time`, `random`, `subprocess`, `socket`, `importlib` и др.), `episteme.domains.api` и относительные импорты внутри пакета; вызовы `__import__`, `eval`, `exec`, `compile`, `breakpoint`, `input` отвергаются. Это проверка контракта для доверенного кода, а не граница безопасности.
- **Отклонение (перенос).** `CasView` и hook-facing типы (`CompileRequest`, `ProtocolContext`, `AnalysisContext`) определены на шаге 2, а не 4: без них conformance suite не может вызвать hooks. `CasView` хранит заранее прочитанные и проверенные bytes allowlist и не держит ссылки на Store; запрос объявленного скрытого входа отвергается с отдельной ошибкой.
- **Уточнение.** Production allowlist на шаге 2 пуст. Conformance suite работает для каждого зарегистрированного пакета и тестового `conformance_fixture_v1`; зарегистрированный пакет без conformance-входов в `tests/pack_fixtures.py` проваливает suite. Программы в suite исполняет минимальный локальный runner без Store; полный путь через ядро — шаг 4.

### Отклонения от предложения и уточнения шага 3

- **Отклонение.** Фасад не импортирует `synthetic_causal` и `synthetic_batch_analysis`. Legacy adapter получает `Store` и импортирует ядро, а пакету разрешены только stdlib и `episteme.domains.api`; импорт модуля вне каталога пакета к тому же вывел бы его bytes из `code_manifest`. Программы, проверки recipe, тексты preregistration и estimator перенесены в пакет дословно. Legacy-модули остаются байт-в-байт прежними: их digests проверяет golden test, а legacy-привязки и replay используют их как раньше. Равенство переноса проверяют тесты: bytes обеих программ и input, design, analysis plan, stopping rule и statistical design на сетке из восьми наборов параметров; на двух сохранённых histories hooks пакета дают те же `details`, statement, limitations, outcome и inference mode, что замороженное legacy предложение.
- **Уточнение.** Host inputs пакета — ровно `world`, `seeds` (1–64 уникальных 32-битных целых) и `replication_tolerance`; world попадает только в `protocol.data`, объявленный `hidden_inputs=["protocol_data"]`. Параметры каталога совпадают с legacy `describe()`. `wall_seconds=120` и `max_output_bytes=1 MiB` взяты из умолчаний модельного пути ADR 0009.
- **Уточнение.** Report пакета: `uncertainty`, `confidence_interval` и `multiple_testing` — `not_applicable` по preregistered design; `effect_size` и `assumptions` — `not_supplied` с причиной; `point_estimate` даёт значения по seeds без объединённой оценки; `sample_size` имеет `unit_scope=per_roster_unit`, `planned=n_samples` и `planned_total=n_samples × число seeds`; `sensitivity_analysis` и `deviations` — пустые списки. Итог тот же, что у legacy: `inconclusive`/`exploratory`.

### Отклонения от предложения и уточнения шага 4

- **Отклонение.** «`domain_binding` v2» и анализ по `AnalysisReport` v2 записываются отдельными kinds `pack_binding` и `pack_analysis`, а не новыми версиями legacy kinds. Replay каждого kind требует точную receipt своего action, поэтому смешать v1 и v2 при replay нельзя. Legacy `domain.bind` и `analysis.apply` отвергают pack-bound protocol, `pack.analyse` отвергает batch без `pack_binding`. Legacy histories не меняются: golden test подтверждает прежние basis, gates, Graph и export.
- **Отклонение (усиление).** `pack.analyse` повторно исполняет analysis hooks закреплённого кода внутри команды, на том же snapshot, и допускает только побайтно равные `OutputCheck`, `Recomputation`, `AnalysisReport` и `StatisticalReport`; событие фиксирует `report_origin=recomputed_by_pinned_pack_at_admission`. Без этого сверка pin доказывала бы лишь наличие закреплённого кода, а не происхождение report. Hooks исполняются и внутри write transaction. Это расходится с правилом «hooks вне транзакции»: v1-пакеты дешёвые и чистые, а тяжёлому будущему пакету потребуется другое решение, например attestation результата.
- **Уточнение.** Compile hooks и `validate_protocol` при `pack.preregister`, а также `validate_protocol` при `batch.plan` и `pack.analyse` тоже исполняются внутри команды: они чистые, без I/O, и их результат должен войти в ту же receipt. Replay структурный и код пакета не импортирует; повторное исполнение исторических hooks — задача `pack verify` (шаг 5).
- **Отклонение (усиление, по итогам внутреннего adversarial review).** На pack-bound protocol `kernel.claim` и ручной `kernel.start_run` отвергаются: claim допускает только `pack.analyse`, запуски идут только через frozen batch. Replay в Graph, export и review assignment отвергает claim на pack-bound protocol без receipt `pack.analyse`. Иначе legacy-команды обходили бы потолок. В legacy-путях без пакетов поведение не изменилось.
- **Уточнение [ADR 0018](0018-claim-families-and-review-admission.md), 4 октября 2026 (аудит, A-03).** Правило выше действовало только для protocol с собственной привязкой, и amendment снимал потолок. Теперь привязка управляет всей линией `parent`. `kernel.preregister`, `kernel.preregister_for_set` и `followup.apply` не создают amendment pack-bound protocol; пути amendment для пакетов пока нет. На потомке без собственной привязки, если такой уже есть в истории, отвергаются `kernel.claim`, ручной `kernel.start_run`, `domain.bind`, `analysis.apply` и `batch.plan`. Новый protocol вне `pack.preregister` не может использовать как implementation, data или split bytes, закреплённые любой привязкой: программы, файлы кода, input и захваченные artifacts. Protocols, созданные до привязки, это правило не затрагивает. Claim на pack-governed protocol без своего `pack_analysis` проваливает mechanical gate; Graph и backup такие истории по-прежнему проверяют.
- **Уточнение.** Потолок применяется так. Расхождение с preregistration, которое находит ядро (estimand, метод и unit uncertainty, interim looks при fixed sample, незапланированные exclusions, analysed ≠ planned_total − exclusions), обязано быть объявлено в `deviations`, иначе анализ отвергается. Это строже, чем «ядро добавляет найденные расхождения»: ядро ничего не дописывает в report, а найденное записывает в `ceiling.detected_deviations`. Расхождение family или correction multiple testing отвергается всегда. `unclassified` для пакетного claim недопустим. К limitations claim ядро добавляет строку о каждом `not_supplied` поле, каждом deviation и итоговую строку потолка. Формулировки потолка v1 заморожены для replay; их изменение потребует `ceiling.schema_version=2`.
- **Уточнение.** Allowlist `CasView` для анализа: объявленные outputs (`raw_data`, `metrics`) всех slots, а также `protocol.data` и захваченные artifacts, если они не объявлены скрытыми. Скрытый digest, совпадающий с наблюдённым output, отвергается. Отказанное чтение записывается в `CasView.refused`; анализ отвергается, даже если пакет перехватил ошибку. Событие хранит `cas_allowlist` и `hidden_digests`.
- **Уточнение (по итогам review).** `ProtocolContext` не содержит host inputs и захваченных bytes: при `batch.plan` и анализе hidden inputs в код пакета больше не передаются. Host inputs (для synthetic это world) лежат в CAS и в payload receipt `pack.preregister`, как прежде `protocol.data`; от hooks анализа и контекста reviewer они скрыты, но секретом не являются. Исключение любого типа из hook превращается в `PackError` с именем hook. Модули файлового I/O и `open()` разрешены только в `capture.py`.
- **Уточнение.** Assignment остаётся `blind_initial_review_v1`: исходник и envelopes пакета, host inputs, report, statistical report, checks и recomputations исключены. Текст report доходит до reviewer только через обязательные строки limitations claim. Полный `StatisticalReport` попадёт в контекст только с политикой v2 (шаг 7).
- **Уточнение.** Ядро записывает факты replication: `same_data_reanalysis`, обе программы из одного закреплённого пакета (один автор), те же raw data; смысл повторов roster выводится из `roster_semantics` (для `rng_seed` — повтор одного генератора с новым seed).
- **Ограничение.** Replay привязки заново читает и хеширует закреплённый код, программы, input и capture; `pack.analyse` и его replay повторно проверяют bindings, batch и receipts и наследуют известную медленную проверку receipts. На synthetic fixture с одним seed весь путь в тестах занимает секунды; для 18 slots afterlife измерение — в шаге 6.

### Отклонения от предложения и уточнения шага 5

- **Уточнение.** `analysis advance` определяет анализ по привязке protocol и для legacy-путей: ручной `domain.bind` даёт записанный в нём `adapter_id`, модельное применение ADR 0008 — `domain` его compilation, `pack_binding` — пакетный путь. `--adapter` больше не выбирает код: без флага используется привязка, а флаг, отличный от неё, отвергается без новых событий. Это заменяет описанное в ADR 0015 поведение «без флага — synthetic adapter». Соответствие legacy `adapter_id` классам legacy-адаптеров остаётся в реестре `LEGACY_ANALYSIS_ADAPTERS`. `cli.py` больше не входит в исключения статического теста.
- **Уточнение.** `pack verify` открывает Store только на чтение. Сначала выполняется структурный replay, затем для каждой привязки — повторные `describe`, compile hooks (сравниваются digests каталога, draft и plan) и `validate_protocol`, для каждого анализа — analysis hooks на префиксе его receipt (сравниваются digests checks, recomputations, report и statistical report). Если живой код отличается от pin, hooks не исполняются, а строка получает `live pack code differs from the pin`. Код возврата 1 означает любое расхождение. `pack describe` Store не открывает.
- **Отклонение (перенос).** Команда захвата `pack capture` добавлена не здесь, а вместе с первым пакетом, которому она нужна, на шаге 6.

### Отклонения от предложения и уточнения шага 6

- **Уточнение.** `episteme pack capture PACK_ID --source DIR --root ROOT` один раз вызывает hook `capture` и записывает bytes и frozen `CaptureBundle` в CAS без событий; источник и research state не должны пересекаться. Захват становится provenance только через `pack.preregister` с digest этого bundle. Для afterlife `source.label` — `run_id` из захваченного manifest.
- **Уточнение.** Аудит исторического run разделён, как и требовал ADR: `capture.py` — единственный модуль пакета с файловым I/O — читает 30 заявленных outputs и manifest, отвергая ссылки, reparse points и незаявленные файлы; чистый `inventory.verify` повторяет все проверки, не требующие файловой системы, по захваченным bytes и при capture, и при compile. Bytes обеих программ и `input.dat` совпадают с legacy `compile_seed_bundle`, множество захваченных digests — с его `recipe_artifacts`, `seen_data` — с декларацией примера ADR 0015. Legacy-модули `afterlife_seed*.py` не менялись.
- **Уточнение.** Проверка объявленной экспозиции (`protocol.data` в `seen_data`, открытый discovery split, 9 исторических траекторий) перенесена из анализа в `validate_protocol` и повторяется при `batch.plan` и `pack.analyse`.
- **Уточнение.** Report: `uncertainty` и `multiple_testing` — `not_applicable` по design; `confidence_interval` и `effect_size` — `not_supplied`, как предписано; `assumptions` — `supplied`: независимость шагов внутри траектории `violated`, репрезентативность за пределами run и точность `finish_reason` — `unchecked`. Итог: `inconclusive`/`exploratory`, 8 limitations пакета, 2 строки ядра о `not_supplied` полях и строка потолка.
- **Уточнение.** Hook называется `capture`, и модуль `capture.py` пакет импортирует под другим именем до определения hook. Иначе импорт подмодуля заменил бы атрибут пакета.
- **Ограничение.** На смешанной истории копии пилота (124 legacy-событий и 119 новых) пакетный путь медленный: 18 jobs `batch advance` — 582 секунды, первый `analysis advance` — 398, повтор — 195, `pack verify` — 45, `graph` — 188 секунд. Read-only `analysis status` занимает 212 секунд для legacy batch и 208 — для пакетного на той же истории. Значит, время уходит на общую повторную проверку receipts, которую пакетный путь наследует; переписывание этой проверки — отдельная задача ядра.

### Отклонения от предложения и уточнения шага 8

Отдельный ADR не добавлен. Потолок силы claim, обязательные поля `StatisticalReport` и запрет confirmatory при просмотренном holdout уже зафиксированы здесь и в [ADR 0018](0018-claim-families-and-review-admission.md) (managed evidence, `seen_data`, нестрогий digest). Этот шаг их исполняет.

- **Отклонение.** Exploratory-фаза с repeated stratified k-fold не реализована. Пакет регистрирует только confirmatory protocol: один roster unit `deterministic_single`, `sample_size_scope=total`. `validate_protocol` отвергает иной режим.
- **Уточнение.** Training CSV и holdout CSV — отдельные файлы захвата и отдельные CAS-артефакты. `seen_data` содержит digest training-файла. Confirmatory split — digest holdout-файла, с ролью `confirmatory` и `exposure_policy=holdout`. Профиль v1 передаёт программе один `input.dat`, поэтому `compile_execution` вкладывает тексты обоих CSV в этот JSON и не подгоняет модель по меткам holdout. Разбор меток для оценки делает runner после записи protocol. Это свойство кода хуков, не граница ОС: bytes лежат в `CaptureBundle` в процессе planner во время compile. Тест меняет только метки holdout и проверяет, что текст design, analysis plan, stopping rule, программы и `seen_data` не меняются, а digest holdout и program input меняются.
- **Уточнение.** Сравнение одно: accuracy фиксированного logistic regression (200 шагов batch gradient descent, learning rate 1, порог 0.5, веса с нуля, стандартизация по training) минус accuracy класса большинства training-меток (ничья → класс 0). Интервал — Wald 95% с фиксированным квантилем `1.959963984540054`. Второе условие — двусторонний exact McNemar по несогласным парам, alpha `0.05`. `supports` только если интервал целиком выше 0 и exact-тест ниже alpha; `refutes` — симметрично ниже 0; иначе `inconclusive`. Семейство multiple testing — одно сравнение, correction `none`.
- **Уточнение.** Таблицы генерирует [`examples/tabular_classification_v1/generate.py`](../../examples/tabular_classification_v1/generate.py): 48 строк, целочисленные `x1,x2,label`. У `planted` метка равна 1 при `x1 >= 0`. У `null` метка всегда 0. У train и holdout разные формулы `x2`, поэтому файлы не совпадают даже после нормализации пробелов. Это не научный набор данных и не копия публичной таблицы. На `planted` правило даёт `supports` (разность 0.5). На `null` разность 0 и исход `inconclusive`. Текст claim говорит, что это правило на захваченных строках, а не эффект в популяции.
- **Уточнение.** В `assumptions` три проверки со статусом `holds`: обучение только на training-файле, полный holdout без исключений, фиксированные квантиль и alpha. Допущение «строки — выборка из популяции» не помечается `holds`: оно записано в limitations. `scientific_validity` остаётся `not_assessed`. Потолок принимает полный confirmatory report и по-прежнему отвергает proposal без interval или с допущением `violated`.
- **Уточнение.** Повторный анализ — вторая программа того же пакета по тем же scored rows. Ядро пишет `single_deterministic_unit`, `same_data_reanalysis`, `context=not_established`. Hooks не получают Store.

### Отклонения от предложения и уточнения шага 9

Отдельный ADR не добавлен. Разделение «ядро допускает переход, пакет проверяет параметры и компилирует план» уже записано здесь и в [ADR 0008](0008-experiment-proposals.md). Этот шаг его исполняет.

- **Отклонение.** Новое admission — action `agent.request_pack_experiment`, а не новая сигнатура `agent.request_experiment`. Command API v1 включает нормализованные defaults в fingerprint. Добавление поля в старую сигнатуру сделало бы исторические receipts schema 2 невоспроизводимыми. Событие по-прежнему `agent_request`, schema_version 3.
- **Уточнение.** Конверт `experiment-proposal-v2` принадлежит ядру: status, reason, limitations, action, predictions, contrast, rationale и components. Пакет объявляет только `proposal_schema` версии 1, и эта схема обязана совпадать с `parameters_schema` каталога. Необязательный `proposal_attempts` считает число попыток по host inputs до выбора параметров моделью. На apply оно обязано совпасть с `run_limit` скомпилированного draft.
- **Уточнение.** Принятое предложение компилируется существующими `compile_protocol` и `compile_execution`. Одна receipt `agent.apply_experiment` пишет `protocol`, `pack_binding`, `experiment_node` и `agent_application` schema 3. Replay привязки структурный и код пакета не импортирует. Текст protocol равен draft пакета: в него не дописывается contrast модели, иначе digest draft разошёлся бы с повторным `compile_protocol`. Contrast и rationale остаются в proposal artifact и узле дерева.
- **Уточнение.** Модель не задаёт `mode`, `outcome` или `inference_mode`. Режим protocol берётся из statistical design пакета. Для `tabular_classification_v1` это `confirmatory` как заранее зарегистрированный дизайн, не как claim. События claim, review и run не создаются. `scientific_validity` остаётся `not_assessed`. Потолок силы claim по-прежнему применяется только в `pack.analyse`.
- **Уточнение.** Host inputs и bytes захвата не входят в контекст модели. Для synthetic это скрытый world. Число попыток в контексте есть, список seeds — нет: schema 2 по-прежнему показывает seeds.
- **Уточнение.** `synthetic_causal_v1` и `tabular_classification_v1` объявляют схему. `afterlife_seed_v1` её не объявляет, и модельный путь его отвергает. На момент шага 9 `proposal.prepare_next` принимал только application schema 2. Шаг 10 принимает schema 3, не меняя fingerprint schema 2.
- **Уточнение.** `agents.py` и `experiment_proposals.py` не импортируют пакет. Статический тест это проверяет. Исключения: `experiment_proposals.py` хранит литерал recipe замороженной schema v1; `legacy_experiment.py` импортирует `domains.synthetic_causal` только для schema 2. Replay уже записанного application schema 2 compiler не вызывает.
- **Ограничение.** Демонстрация — fixture-ответ, уже совпадающий со схемой пакета. Сети и вызова модели нет. Выход не является научным результатом. Полная матрица универсальности остаётся шагом 10.

### Отклонения от предложения и уточнения шага 10

Отдельный ADR не добавлен. Допуск review и paper уже заданы [ADR 0018](0018-claim-families-and-review-admission.md). Этот шаг проводит зарегистрированные пакеты по тому же пути и не ослабляет его.

- **Уточнение.** Fixture review в тестах — цепочка `review.assign` → выдача подготовленного ответа → `review.submit`. Rationale записан как явное тестовое мнение. `scientific_validity` на событиях остаётся `not_assessed`. Manuscript говорит, что scaffold не является статьёй к подаче и что counted approval не оценивает научную состоятельность. Для всех трёх пакетов `next_action` после такого approval равен `paper_candidate`: mechanical gate проходит, veto и obligations нет, policy назначения — `blind_initial_review_v2`. Это допуск записи, не научное одобрение. ID reviewer заявляет тест; контекст независимости остаётся `not_established`.
- **Уточнение.** `proposal.prepare_next` принимает и application schema 2, и schema 3. Сигнатура команды и её defaults не менялись, поэтому fingerprint schema 2 прежний. Для schema 3 reanalysis source, environment, outputs и лимиты берутся из закреплённого execution plan. Если вызывающий явно передаёт лимиты, отличные и от defaults schema 2, и от pin, команда отвергается без событий. Совпадение с defaults schema 2 не переписывает pin: у `tabular_classification_v1` batch получает `wall_seconds=60`. `afterlife_seed_v1` схему предложения не объявляет, и этот переход его не принимает.
- **Уточнение.** Колонка roster в export и paper подписывается значением `roster_semantics`, если все протоколы таблицы закреплены одним и тем же значением. Иначе заголовок остаётся `Seed`, чтобы истории без пакета, включая golden, не меняли текст. Рядом показываются уже записанные `replication` и список `not_supplied`. Новый статистический расчёт не добавлялся.
- **Пробел.** Пункт 4 требует одинаковый отказ при чтении hidden input. Его можно показать только там, где пакет объявил скрытый вход. Делать вид, что пустой `hidden_inputs` даёт ту же ошибку, этот шаг не стал.
- **Пробел.** `partition_seed` остаётся значением перечисления без зарегистрированного протокола.
- **Ограничение.** Paper scaffold из fixture review не является внешним peer review и не меняет `scientific_validity`. Проверки этого шага не покрывают пакеты вне allowlist. `locked_fixture_v2` в реестр не входит. Изоляция, literature, controller loop и открытые пункты аудита хранения не закрываются.

## Ограничения

Контракт не создаёт изоляции: доверенный пакет может читать всё и вычислить статистику неверно. Report проверяет наличие и согласованность полей, а не их правильность; её устанавливают независимая реализация и review. Отказ анализа воспроизводим из закреплённых bytes и детерминированных hooks, но в v1 не записывается отдельным событием; это нужно решить до автоматического controller loop. `capture` доверяет локальной файловой системе: hashes фиксируют захваченные bytes, а не историческое время или полноту источника. Runner остаётся stdlib-only, поэтому пакеты с зависимостями, GPU или вызовами provider требуют нового execution profile. Ни fixture e2e, ни conformance suite не являются scientific review и не подтверждают универсальность за пределами проверенных пакетов.
