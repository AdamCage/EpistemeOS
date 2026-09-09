# EpistemeOS

Research harness для вычислительных научных исследований: конкурирующие объяснения → выбор эксперимента → воспроизводимое evidence → независимое review → новый план → публикационный пакет.

**Статус: начальная реализация, не готовый автономный AI Scientist.** Исследованы `llm-semantic-afterlife`, AI Scientist, Kosmos, Virtual Lab, Robin и AI Co-Scientist; спроектирована архитектура и реализовано проверяемое локальное ядро. Публикационное качество и превосходство над другими системами пока не оценены.

## Начать с документов

- [Архитектура и границы гарантий](docs/architecture.md).
- [MVP-план с критериями приёмки](docs/mvp-plan.md) и [структура репозитория](docs/repository-map.md).
- [Аудит исходного afterlife](docs/research/afterlife-audit.md).
- [AI Scientist / AI Co-Scientist](docs/research/ai-scientist-coscientist.md), [Kosmos / Virtual Lab / Robin](docs/research/kosmos-virtual-lab-robin.md).
- [План оценки научной результативности](docs/research/evaluation-plan.md) и [инженерное review](docs/implementation-review.md).
- [Текущая проверка, runs и оставшиеся ограничения](docs/validation.md).
- [Идемпотентные команды v1](docs/command-api.md) и [статистический протокол / exposure](docs/decisions/0002-statistical-design.md).

## Локальный запуск

Python 3.11 или новее. У ядра нет runtime-зависимостей вне стандартной библиотеки. Из корня проекта:

```powershell
uv sync
uv run episteme demo --root .research/demo
uv run episteme inspect --root .research/demo
uv run episteme export --root .research/demo
uv run episteme demo --with-search --root .research/search-demo
uv run python -m unittest discover -s tests -v
```

Без uv можно создать virtualenv и выполнить `python -m pip install -e .`. Для запуска прямо из исходников в PowerShell: `$env:PYTHONPATH = 'src'`, затем `python -m episteme demo --root .research/demo`.

Demo выполняет три реальных CPU-вычисления на синтетических данных и три повторных анализа другой формулой в отдельных Python-процессах. Сохраняет код, inputs, среду, raw CSV, метрики и логи; останавливается на `scientific_review`. Это fixture с заданными ролями, а не независимые LLM-агенты или новое научное открытие. Повторный запуск требует новой пустой папки.

Флаг `--with-search` добавляет сохраняемый турнир с перестановкой A/B, два варианта эксперимента и best-first выбор под бюджетом. Неисполненная альтернатива остаётся в дереве. Судейские оценки и компоненты приоритета заданы fixture-кодом; рейтинг не является оценкой научной истинности.

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

Verdicts: `approve`, `request_changes`, `reject`. `approve` требует пустого списка незакрытых actions. Reviewer с ID автора протокола, claim или любого связанного run не допускается. Отрицательное мнение одного reviewer не отменяется одобрением другого. Новое evidence инвалидирует прежний review basis.

После актуального approval можно собрать **внутренний черновик**:

```powershell
uv run episteme paper <claim-id> --root .research/demo --title "Название исследования" --actor writer-1
```

Он содержит только выбранные claims, зарегистрированные методы, точные метрики, ссылки на evidence и ограничения. Сохраняются immutable Markdown/JSON artifacts и paper event. Литературный обзор, проверка метода против кода, научный вклад, venue formatting и внешнее peer review остаются обязательной дальнейшей работой. CLI ничего не публикует.

## Что уже обеспечивается

- SHA-256 artifacts, проверяемый event hash chain и атомарная запись с expected revision.
- Versioned `command` API: атомарные events/receipts, replay после потери ответа, conflict при изменении body, проверки конкурирующих writers.
- Protocol до RunStarted, immutable amendments, фиксированные inputs/code/environment и seed schedule.
- Typed statistical design, exploratory/confirmatory режим, declarative exposure ledger и запрет повторного объявления просмотренных bytes свежим holdout.
- Сохранение failed/cancelled attempts, лимит числа runs и gates на полноту всех результатов.
- Claim scope и run references, проверка повторного анализа, отклонение self-review/self-replication по ID.
- Snapshot-consistent export, актуальность evidence для review, veto отрицательного review и блокировка premature paper.
- Persistent tournament/tree, воспроизводимый replay решений, проверка актуальности frontier и общий лимит технических retries.

## Граф и исторический импорт

```powershell
uv run episteme graph --root .research/search-demo
uv run episteme graph --root .research/search-demo --format dot
uv run episteme afterlife inspect C:\Projects\llm-semantic-afterlife --max-verify-mib 64
uv run episteme afterlife import C:\Projects\llm-semantic-afterlife --root .research/afterlife-history --max-verify-mib 64
```

Граф проверяет ссылки и байты артефактов; Python API `ResearchGraph` поддерживает ancestors/descendants и точный фильтр claims по scope. Рёбра отражают зарегистрированные зависимости, не автоматически установленную истинность.

Afterlife importer сохраняет immutable исторический снимок, статусы и ограничения проверки. Повторный импорт того же снимка идемпотентен. По умолчанию копируются metadata, а большие outputs только проверяются по hashes в пределах лимита; непроверенные ссылки остаются явными. Импорт не создаёт preregistered protocols, reviews или accepted claims и не меняет исходный checkout. Это начало domain adapter; перенос исполнения/анализа afterlife остаётся в плане.

Actor IDs пока назначает доверенный вызывающий процесс. Разные ID и source hashes не доказывают независимость рассуждения или clean-room реализацию. SQLite/hash chain не защищает от владельца файлов. Generic kernel проверяет наличие и согласованность метрик и статистических деклараций; соответствие фактических данных, вычисление uncertainty и научную корректность метода должен проверять domain adapter и независимая реализация. Полноценные агенты, sandbox и durable execution/replanning перечислены в MVP-плане.

Сохранённый исходный [objective.md](objective.md) остаётся контекстом проекта. Прямое сравнительное утверждение «лучше существующих AI Scientist систем» будет допустимо только после контролируемой оценки.
