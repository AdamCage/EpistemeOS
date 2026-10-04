# EpistemeOS

Research harness для вычислительных научных исследований: конкурирующие объяснения → выбор эксперимента → воспроизводимое evidence → независимое review → новый план → публикационный пакет.

**Статус: начальная реализация, не готовый автономный AI Scientist.** Исследованы `llm-semantic-afterlife`, AI Scientist, Kosmos, Virtual Lab, Robin и AI Co-Scientist; спроектирована архитектура и реализовано проверяемое локальное ядро. Публикационное качество и превосходство над другими системами пока не оценены.


Первый агентный task реализован через [Codex CLI adapter](docs/decisions/0007-model-proposals.md): сохранённый вопрос → ответ модели → проверенные по форме hypotheses/ExplanationSet. Второй [task предложения эксперимента](docs/decisions/0008-experiment-proposals.md) принимает frozen ExplanationSet/tree и создаёт exploratory protocol плюс новый tree node из допустимого ответа. Failed, invalid и abstained responses сохраняются; после неизвестного исхода автоматического повторного вызова нет. Это trusted local execution, без файлового/сетевого sandbox, доказанной независимости или научного approval.

Подготовка одного задания (уже установленный и авторизованный Codex CLI, model указывается явно):

```powershell
uv run python examples/model_hypotheses.py --root .research/model-proposal --model <model-id>
uv run episteme agent advance <agent-request-id> --root .research/model-proposal
```

Первая команда делает только version probe и сохраняет задание; вторая вызывает модель и применяет допустимое предложение. `agent work` сохраняет response без application; `agent status` читает состояние; `agent reconcile` импортирует исходный completion без нового вызова. Пример ограничен одним admission и 120 секундами; это не hard token/денежный budget. Реальные результаты и ограничения — в [validation.md](docs/validation.md).

Для второго task после создания вопроса, ExplanationSet и Search tree с `cost_unit="enqueued_attempt"` host замораживает synthetic recipe. `recipe.json` содержит ровно `world`, `seeds`, `environment` и `replication_tolerance`; environment digest получают через `execution environment`:

```json
{"world":{"treatment_effect":1.0,"confounding_strength":0.5,"noise_std":1.0},"seeds":[7,11],"environment":"<environment-digest>","replication_tolerance":0.1}
```

Затем versioned command `agent.request_experiment` связывает budget, ExplanationSet, tree, recipe binding, assignee и provider; `agent advance` применяет допустимый ответ через `agent.apply_experiment`.

```powershell
uv run episteme execution environment --root .research/study
uv run episteme agent recipe --input recipe.json --root .research/study
uv run episteme agent provider --model <model-id> --root .research/study
uv run episteme command --input request-experiment.json --root .research/study
uv run episteme agent advance <agent-request-id> --root .research/study
```

Первые две команды только сохраняют host-owned descriptors, без model call или научных событий. В `recipe.json` `environment` — digest из первой команды; в `request-experiment.json` используется [command envelope](docs/command-api.md) и IDs, возвращённые предыдущими шагами. [Публичная schema ответа](schemas/experiment-proposal-v1.schema.json) дополняется проверкой полного порядка frozen hypotheses. Значения `world` не входят в prompt модели, но сохраняются в локальном CAS; это не секретный sandbox. Применение не выбирает узел, не исполняет его и не создаёт evidence, review или paper. Следующая команда `proposal.prepare_next` через [command API](docs/command-api.md) атомарно выбирает winning applied proposal и готовит полный batch primary/reanalysis; она не запускает worker. После сохранения batch ID можно продолжить локальный synthetic pilot:

```powershell
uv run episteme batch advance <batch-id> --root .research/study
uv run episteme analysis status <batch-id> --root .research/study
uv run episteme analysis advance <batch-id> --root .research/study --planner planner-1 --analyst analyst-1 --reviewer reviewer-1
```

`analysis advance` пересчитывает метрику из наблюдённых raw data, сохраняет bounded exploratory claim и назначает reviewer на текущем evidence basis. Команда `analysis.apply` допускает только proposal, который зарегистрированный адаптер заново вычисляет на том же снимке ([ADR 0018](docs/decisions/0018-claim-families-and-review-admission.md)): подставить собственный вывод под ID адаптера нельзя. Повтор после перезапуска не создаёт второй claim или assignment. Статус `awaiting_review` не означает научное подтверждение; demo и CLI не создают reviewer verdict. Контракт — [ADR 0013](docs/decisions/0013-batch-analysis-admission.md), фактические проверки — в [validation.md](docs/validation.md).

Контракт DomainPack ([ADR 0016](docs/decisions/0016-domain-pack-contract.md)) добавляет `pack.preregister`: одна receipt создаёт protocol и привязку, закрепляющую digest всего кода пакета, каталог, compiled draft и execution plan. `analysis advance` для такого batch вызывает hooks закреплённого пакета, повторно исполняет их внутри `pack.analyse` и допускает claim только в пределах потолка силы, который вычисляет ядро. Пакет и adapter теперь всегда берутся из привязки protocol; `--adapter` лишь утверждает их и отвергается при расхождении. Проверить живой код и записанные bytes без записи в Store:

```powershell
uv run episteme pack describe synthetic_causal_v1
uv run episteme pack verify --root .research/study
```

Пакет — доверенный локальный Python в процессе ядра, а не изолированный plugin.

Ручной `domain.bind` через `episteme command` фиксирует domain recipe, исходник адаптера и параметры batch до исполнения planning-bound protocol. Его используют synthetic adapter и офлайн-пилот на исторических траекториях Afterlife. Сам historical import по-прежнему не создаёт анализируемые runs или claim. Контракты — [ADR 0014](docs/decisions/0014-manual-domain-binding.md) и [ADR 0015](docs/decisions/0015-afterlife-historical-pilot.md).

## Начать с документов

- [Архитектура и границы гарантий](docs/architecture.md).
- [MVP-план с критериями приёмки](docs/mvp-plan.md) и [структура репозитория](docs/repository-map.md).
- [Аудит исходного afterlife](docs/research/afterlife-audit.md).
- [AI Scientist / AI Co-Scientist](docs/research/ai-scientist-coscientist.md), [Kosmos / Virtual Lab / Robin](docs/research/kosmos-virtual-lab-robin.md).
- [План оценки научной результативности](docs/research/evaluation-plan.md) и [инженерное review](docs/implementation-review.md).
- [Текущая проверка, runs и оставшиеся ограничения](docs/validation.md).
- [Идемпотентные команды v1](docs/command-api.md) и [статистический протокол / exposure](docs/decisions/0002-statistical-design.md).
- [Связи claims, зависимый review и supersession](docs/decisions/0003-claim-relations.md).
- [Переносимый backup и восстановление состояния](docs/recovery.md).
- [Версии исследовательского вопроса и explanation sets](docs/decisions/0004-planning-lineage.md).
- [Локальный runner, однократный dispatch и восстановление результата](docs/decisions/0005-local-runner.md).
- [Выбранный эксперимент, полный набор запусков и восстановление controller](docs/decisions/0006-execution-batches.md).
- [Модельное предложение эксперимента и host-owned synthetic recipe](docs/decisions/0008-experiment-proposals.md).
- [Подготовка batch и сохраняемый follow-up по отрицательному review](docs/decisions/0009-proposal-review-replanning.md).
- [Анализ завершённого batch и назначение reviewer](docs/decisions/0013-batch-analysis-admission.md).
- [Ручная привязка frozen domain recipe до batch](docs/decisions/0014-manual-domain-binding.md).
- [Разведочный офлайн-пилот на исторических траекториях Afterlife](docs/decisions/0015-afterlife-historical-pilot.md).
- [Контракт DomainPack: envelopes, statistical report и потолок силы claim](docs/decisions/0016-domain-pack-contract.md) — принят 4 октября 2026 и реализуется поэтапно; текущий статус шагов указан в самом ADR.
- [Инкрементальная проверка цепочки событий и receipts](docs/decisions/0017-incremental-verification.md).
- [Состязательный аудит коммита `da6aa2a`](docs/adversarial-audit-2026-10-04.md) от 4 октября 2026: 23 воспроизведённые находки (6 high, 7 medium, 10 low), PoC и статус исправлений.
- [Семейства claims и допуск reviews к решениям](docs/decisions/0018-claim-families-and-review-admission.md) — исправления семантических находок аудита; принят 4 октября 2026 и реализуется поэтапно. Пока шаги не выполнены, находки A-01–A-10, A-14 и A-22 остаются открытыми; статус указан в аудите и в самом ADR.

## Локальный запуск

Python 3.11 или новее. У ядра нет runtime-зависимостей вне стандартной библиотеки. Из корня проекта:

```powershell
uv sync
uv run episteme demo --root .research/demo
uv run episteme inspect --root .research/demo
uv run episteme export --root .research/demo
uv run episteme demo --with-search --root .research/search-demo
uv run python examples/local_execution.py --root .research/local-execution-example
uv run python examples/search_execution_batch.py --root .research/search-batch-example
uv run python -m unittest discover -s tests -v
```

Без uv можно создать virtualenv и выполнить `python -m pip install -e .`. Для запуска прямо из исходников в PowerShell: `$env:PYTHONPATH = 'src'`, затем `python -m episteme demo --root .research/demo`.

Demo выполняет три реальных CPU-вычисления на синтетических данных и три повторных анализа другой формулой в отдельных Python-процессах. Сохраняет код, inputs, среду, raw CSV, метрики и логи; останавливается на `scientific_review`. Это fixture с заданными ролями, а не независимые LLM-агенты или новое научное открытие. Повторный запуск требует новой пустой папки.

Флаг `--with-search` добавляет сохраняемый турнир с перестановкой A/B, два варианта эксперимента и best-first выбор под бюджетом. Неисполненная альтернатива остаётся в дереве. Судейские оценки и компоненты приоритета заданы fixture-кодом; рейтинг не является оценкой научной истинности.

`examples/local_execution.py` использует общий runner: создаёт frozen Python jobs, выполняет primary и отдельный повторный анализ, сохраняет manifests и останавливается перед scientific review. Runner поддерживает `execution status`, `execution work` и `execution reconcile`; неизвестный исход после dispatch не запускается повторно. Это trusted local backend без filesystem/network sandbox. У среды фиксируется fingerprint интерпретатора/ОС, а не переносимый полный environment bundle.

`examples/search_execution_batch.py` связывает Search с runner: замораживает primary/reanalysis для всех seeds и резервирует число attempts. `episteme batch advance <batch-id> --root <directory>` продолжает сохранённый набор; unknown удерживает резерв, а failed primary оставляет зависимый переанализ заблокированным. Completed batch останавливается на `awaiting_analysis`, без claim или review. DB/CAS restore сохраняет evidence, но не локальный token разрешения новых запусков batch.

Результат в выбранном `--root`:

```text
state.sqlite3                 append-only события и связи evidence
artifacts/sha256/<digest>      проверяемые исходные и производные артефакты
events.jsonl                  переносимый снимок журнала
review-bundle.json            протоколы, claims, runs, gates, hash inventory
report.md                     читаемый отчёт с первичными метриками
```

CLI печатает `claim` и `basis_hash`. Проверить конкретный claim:

```powershell
uv run episteme gate <claim-id> --root .research/demo
```

Код возврата gate: `0` — формальные условия выполнены, `1` — нарушение/ошибка. Научную истинность gate не оценивает. `inspect`, `gate` и `export` открывают SQLite в read-only режиме; export создаёт производные файлы, не меняя журнал.

Для переноса состояния вместе с artifact bytes и command receipts:

```powershell
uv run episteme backup --root .research/demo --output .research/demo-snapshot
uv run episteme restore .research/demo-snapshot --root .research/demo-restored
```

Назначение должно быть новым каталогом. Snapshot сохраняет исходные IDs/hashes и повторную доставку команд; он не перезапускает процессы и не восстанавливает среду эксперимента. JSONL export отдельно от SQLite не сохраняет idempotency.

## Review и paper scaffold

Внешний рецензент сначала читает frozen protocol и raw evidence, затем готовит JSON:

```json
{
  "reviewer_id": "reviewer-methods-1",
  "expected_basis": "<basis_hash из gate>",
  "verdict": "request_changes",
  "rationale": "Причина решения, привязанная к evidence и области применимости.",
  "actions": ["Конкретное дополнительное измерение или сужение claim."]
}
```

```powershell
uv run episteme review <claim-id> --root .research/demo --input review.json
```

Verdicts: `approve`, `request_changes`, `reject`. `approve` требует пустого списка незакрытых actions. Reviewer с ID участника evidence context, включая авторов гипотез и связей, не допускается. Отрицательное мнение одного reviewer не отменяется одобрением другого или изменением basis. Его veto и открытые obligations действуют на всё семейство claim: claims на том же protocol, на protocols той же линии amendments и follow-ups, на protocols с общими наблюдёнными bytes и связанные `supersedes` ([ADR 0018](docs/decisions/0018-claim-families-and-review-admission.md)). Поэтому повторный claim на тех же evidence их не обходит. Resolution obligation засчитывается только для claim, который оценил reviewer. Новое evidence инвалидирует прежний review basis. Для claims со связями нужен action `kernel.review_with_links` через [command API](docs/command-api.md), с явной оценкой каждой связи и открытых замечаний связанного контекста.

Новый `replanning.record_review` принимает отрицательный verdict и typed findings с точными `evidence_refs` и `closure_criterion`. Он сохраняет review и открытые obligations одной command receipt. Planner может применить `followup.apply` к одному `discriminating_experiment` obligation: создать дочерний frozen protocol/tree node на актуальном basis. Затем `followup.prepare_next` атомарно выбирает этот узел, если он выигрывает текущий tree search, и готовит batch по явному frozen recipe. После исполнения и анализа исходный reviewer может вызвать `replanning.resolve_obligation`: одобрить новый ограниченный claim и адресно зафиксировать удовлетворение одного finding на immutable evidence basis с citations на новые results. Поздняя evidence/review revision делает это решение stale и вновь блокирует paper; другие открытые findings исходного claim также блокируют потомка. ID reviewer и planner задаёт доверенный caller; независимый Scientific Reviewer agent здесь ещё не реализован.

Planner может отдельно вызвать `review.assign` с `claim`, `reviewer_actor` и текущим `expected_basis` через [command API](docs/command-api.md). Одна receipt сохраняет assignment и CAS manifest предполагаемого начального контекста: вопрос, конкурирующие объяснения, frozen protocol, claim и наблюдённые results; из artifact allowlist доступны только `raw_data` и `metrics`. Прямые source/environment/log digests и прежние review verdicts исключены. Это проверяемая спецификация выдачи, **не** аутентификация reviewer и не ограничение чтения локального Store. Существующие review commands пока не требуют assignment. Контракт — [ADR 0011](docs/decisions/0011-review-assignment-context.md).

Следующий локальный срез сохраняет выдачу этого пакета через `ReviewerController`: `review.dispatch` фиксирует frozen запрос до вызова provider, `review.finalize` сохраняет raw ответ либо failure, а потерянный исход остаётся `unknown` без автоматического повтора. `review.submit` допускает verdict только из завершённого ответа конкретного assignment на текущем evidence basis; отрицательный ответ создаёт typed открытые obligations. Контракты и [schema ответа](schemas/review-response-v1.schema.json) описаны в [ADR 0012](docs/decisions/0012-review-delivery-and-submission.md). Это provenance локальных, caller-declared ролей: provider не заперт в OS sandbox, его личность и научная независимость не подтверждены. Demo по-прежнему останавливается до научного review и не фабрикует approval.

Все открытые findings исходного claim также блокируют черновик claim на протоколе дочернего follow-up даже при положительном review этого нового claim.

После актуального approval можно собрать **внутренний черновик**:

```powershell
uv run episteme paper <claim-id> --root .research/demo --title "Название исследования" --actor writer-1
```

Он содержит выбранные claims, зарегистрированные методы, точные метрики, ссылки на evidence и ограничения. Для claim на дочернем follow-up protocol Markdown и JSON показывают цепочку исходного отрицательного review, замечания, frozen follow-up, нового evidence и адресного решения reviewer; другие findings сохраняются в полном review bundle и блокируют преждевременный draft. Связанные конкурирующие/прежние claims показываются с их фактическим статусом. Сохраняются immutable Markdown/JSON artifacts и paper event. Литературный обзор, проверка метода против кода, научный вклад, venue formatting и внешнее peer review остаются обязательной дальнейшей работой. CLI ничего не публикует.

## Что уже обеспечивается

- SHA-256 artifacts, проверяемый event hash chain и атомарная запись с expected revision.
- Versioned `command` API: атомарные events/receipts, replay после потери ответа, conflict при изменении body, проверки конкурирующих writers.
- Согласованный SQLite backup вместе с CAS, проверка полного snapshot и восстановление в новый Store с исходными receipts.
- Protocol до RunStarted, immutable amendments, фиксированные inputs/code/environment и seed schedule.
- Версии ResearchQuestion и ExplanationSet, frozen protocol binding, причины исключения кандидатов и сохранение planning ancestry.
- Typed statistical design, exploratory/confirmatory режим, declarative exposure ledger и запрет повторного объявления просмотренных bytes свежим holdout.
- Сохранение failed/cancelled attempts, лимит числа runs и gates на полноту всех результатов.
- Claim scope и run references, проверка повторного анализа, отклонение self-review/self-replication по ID.
- Immutable `supports/contradicts/limits/supersedes` links, транзитивный evidence context, review v2 и сохранение прежних claims/замечаний.
- Snapshot-consistent export, актуальность evidence для review, veto отрицательного review и открытых obligations для всего семейства claim и блокировка premature paper.
- Persistent tournament/tree, воспроизводимый replay решений, проверка актуальности frontier и общий лимит технических retries.
- Два versioned model proposal tasks: hypotheses/ExplanationSet и exploratory protocol/tree node, с original response, receipts и provenance.
- Атомарная подготовка полного batch для выбранного applied proposal; typed отрицательные reviewer obligations и сохраняемый дочерний follow-up без ложного закрытия finding.
- Receipt-backed анализ завершённого synthetic batch до ограниченного claim и назначение reviewer на frozen basis; scientific verdict не создаётся.
- Офлайн-пересчёт доли stop-событий девяти уже наблюдённых траекторий Afterlife через общий runner: полная проверка захваченных bytes и записей шагов, exploratory protocol с объявленной экспозицией и `inconclusive` claim до review.

## Граф и исторический импорт

```powershell
uv run episteme graph --root .research/search-demo
uv run episteme graph --root .research/search-demo --format dot
uv run episteme afterlife inspect C:\Projects\llm-semantic-afterlife --max-verify-mib 64
uv run episteme afterlife import C:\Projects\llm-semantic-afterlife --root .research/afterlife-history --max-verify-mib 64
```

Граф проверяет ссылки и байты артефактов; Python API `ResearchGraph` поддерживает ancestors/descendants и точный фильтр claims по scope. Рёбра отражают зарегистрированные зависимости, не автоматически установленную истинность.

Afterlife importer сохраняет immutable исторический снимок, статусы и ограничения проверки. Повторный импорт того же снимка идемпотентен. По умолчанию копируются metadata, а большие outputs только проверяются по hashes в пределах лимита; непроверенные ссылки остаются явными. Импорт не создаёт preregistered protocols, reviews или accepted claims и не меняет исходный checkout.

Отдельный офлайн-пилот пересчитывает долю stop-событий из девяти уже сохранённых траекторий одного run. Укажите каталог этого run; `prepare` проверит все 30 объявленных outputs и создаст новый exploratory protocol и batch, но не запустит его:

```powershell
uv run python examples/afterlife_historical_pilot.py prepare --source-run <run-directory> --root .research/afterlife-pilot
uv run episteme batch advance <batch-id-from-prepare> --root .research/afterlife-pilot
uv run episteme analysis advance <batch-id-from-prepare> --root .research/afterlife-pilot --planner afterlife-pilot-planner --analyst afterlife-pilot-analyst --reviewer afterlife-pilot-reviewer
uv run episteme analysis status <batch-id-from-prepare> --root .research/afterlife-pilot
```

Тот же run можно провести через пакет `afterlife_seed_v1`: `episteme pack capture afterlife_seed_v1 --source <run-directory> --root <state>` один раз читает 30 заявленных outputs и сохраняет их в CAS без событий, затем `pack.preregister` через `episteme command` получает digest захвата, пустые `parameters` и `host_inputs`, а `batch advance` и `analysis advance` работают как выше. Захват не создаёт preregistration задним числом: экспозиция исторических данных объявляется в protocol.

Запуски локальные и не вызывают provider. Второй анализ использует те же исходные шаги; `awaiting_review` оставляет научное решение открытым. В выбранных файлах нет embeddings для повторного вычисления исходного S1 семантического разрыва. [ADR 0015](docs/decisions/0015-afterlife-historical-pilot.md) описывает данные, проверки и ограничения.

Actor IDs пока назначает доверенный вызывающий процесс. Разные ID и source hashes не доказывают независимость рассуждения или clean-room реализацию. SQLite/hash chain не защищает от владельца файлов. Generic kernel проверяет наличие и согласованность метрик и статистических деклараций; соответствие фактических данных, вычисление uncertainty и научную корректность метода должен проверять domain adapter и независимая реализация. Независимые Executor, Replication и Scientific Reviewer агенты, sandbox, автоматическое исполнение follow-up и внешне проверяемое закрытие научных замечаний остаются в [MVP-плане](docs/mvp-plan.md).

Сохранённый исходный [objective.md](objective.md) остаётся контекстом проекта. Прямое сравнительное утверждение «лучше существующих AI Scientist систем» будет допустимо только после контролируемой оценки.
